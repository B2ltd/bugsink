"""Manual issue merge (bugsink#167).

Keeps the chosen winner issue, moves events/groupings from losers onto it, soft-deletes
the losers, records a MERGED TurningPoint, and fires send_merge_alert for opted-in
messaging services.
"""

from __future__ import annotations

import json

from django.db.models import Max, Min
from django.utils import timezone

from bugsink.transaction import delay_on_commit, immediate_atomic
from bugsink.utils import assert_
from snappea.decorators import shared_task

MERGE_BATCH_SIZE = 500


def validate_merge(winner, losers):
    assert_(losers, "Select at least one other issue to merge")
    loser_ids = {issue.id for issue in losers}
    assert_(winner.id not in loser_ids, "Winner cannot also be absorbed")
    for issue in losers:
        assert_(issue.project_id == winner.project_id, "Cannot merge across projects")
        assert_(not issue.is_deleted, "Cannot merge a deleted issue")
    assert_(not winner.is_deleted, "Cannot merge into a deleted issue")


def kickoff_merge(winner, losers, user=None):
    validate_merge(winner, losers)
    absorbed = [issue.friendly_id() for issue in losers]
    delay_on_commit(
        merge_issues,
        str(winner.id),
        [str(issue.id) for issue in losers],
        None if user is None else user.id,
        absorbed,
    )


def merge_issues_batch(winner_id, loser_ids) -> bool:
    """Move up to MERGE_BATCH_SIZE events from losers onto winner. Returns True if more remain."""
    from events.models import Event
    from issues.models import Issue
    from tags.models import EventTag

    with immediate_atomic():
        max_do = Event.objects.filter(issue_id=winner_id).aggregate(m=Max("digest_order"))["m"] or 0
        events = list(
            Event.objects.filter(issue_id__in=loser_ids)
            .order_by("timestamp", "id")
            .values_list("id", flat=True)[:MERGE_BATCH_SIZE]
        )
        if not events:
            return False

        for offset, event_id in enumerate(events):
            new_do = max_do + 1 + offset
            EventTag.objects.filter(event_id=event_id).update(issue_id=winner_id, digest_order=new_do)
            Event.objects.filter(id=event_id).update(issue_id=winner_id, digest_order=new_do)

        winner = Issue.objects.get(id=winner_id)
        new_max = max_do + len(events)
        # digested_event_count tracks ingest assignments; keep it at least max DO so ingest appends after merges.
        if winner.digested_event_count < new_max:
            winner.digested_event_count = new_max
        winner.stored_event_count = Event.objects.filter(issue_id=winner_id).count()
        bounds = Event.objects.filter(issue_id=winner_id).aggregate(
            first=Min("ingested_at"),
            last=Max("ingested_at"),
        )
        if bounds["first"]:
            winner.first_seen = bounds["first"]
        if bounds["last"]:
            winner.last_seen = bounds["last"]
        winner.save(update_fields=["digested_event_count", "stored_event_count", "first_seen", "last_seen"])
        return True


def _merge_issue_tags(winner_id, loser_ids):
    from tags.models import IssueTag

    for tag in IssueTag.objects.filter(issue_id__in=loser_ids):
        existing = IssueTag.objects.filter(issue_id=winner_id, value_id=tag.value_id).first()
        if existing is None:
            tag.issue_id = winner_id
            tag.save(update_fields=["issue_id"])
            continue
        existing.count += tag.count
        existing.save(update_fields=["count"])
        tag.delete()


def _merge_hourly_counts(winner_id, loser_ids):
    from events.models import IssueEventCountsPerHour

    for row in IssueEventCountsPerHour.objects.filter(issue_id__in=loser_ids):
        existing = IssueEventCountsPerHour.objects.filter(issue_id=winner_id, bucket=row.bucket).first()
        if existing is None:
            row.issue_id = winner_id
            row.save(update_fields=["issue_id"])
            continue
        existing.count += row.count
        if row.digest_order and (existing.digest_order is None or row.digest_order > existing.digest_order):
            existing.digest_order = row.digest_order
        existing.save(update_fields=["count", "digest_order"])
        row.delete()


def _merge_external_issues(winner_id, loser_ids):
    from issues.models import ExternalIssue

    for link in ExternalIssue.objects.filter(issue_id__in=loser_ids):
        clash = ExternalIssue.objects.filter(
            issue_id=winner_id, provider=link.provider, identifier=link.identifier).exists()
        if clash:
            link.delete()
            continue
        link.issue_id = winner_id
        link.save(update_fields=["issue_id"])


def _merge_issue_metadata(winner, losers):
    merged = dict(winner.parsed_metadata())
    changed = False
    for loser in losers:
        for key, value in loser.parsed_metadata().items():
            if key not in merged:
                merged[key] = value
                changed = True
    if changed:
        winner.set_metadata(merged, merge=False)
        winner.save(update_fields=["metadata"])


def finalize_merge(winner_id, loser_ids, user_id, absorbed_friendly_ids):
    from alerts.tasks import send_merge_alert
    from events.models import Event
    from issues.models import (
        Grouping,
        Issue,
        TurningPoint,
        TurningPointKind,
        mark_issues_for_deletion,
    )
    from issues.tasks import delete_issue_deps

    with immediate_atomic():
        assert_(
            not Event.objects.filter(issue_id__in=loser_ids).exists(),
            "Cannot finalize merge while loser events remain",
        )

        winner = Issue.objects.get(id=winner_id)
        losers = list(Issue.objects.filter(id__in=loser_ids, is_deleted=False))
        if not losers:
            return

        _merge_issue_tags(winner_id, loser_ids)
        _merge_hourly_counts(winner_id, loser_ids)
        _merge_external_issues(winner_id, loser_ids)
        _merge_issue_metadata(winner, losers)

        Grouping.objects.filter(issue_id__in=loser_ids).update(issue_id=winner_id)
        TurningPoint.objects.filter(issue_id__in=loser_ids).update(issue_id=winner_id)

        user = None
        if user_id is not None:
            from django.contrib.auth import get_user_model

            user = get_user_model().objects.filter(id=user_id).first()

        TurningPoint.objects.create(
            project_id=winner.project_id,
            issue=winner,
            triggering_event=None,
            user=user,
            timestamp=timezone.now(),
            kind=TurningPointKind.MERGED,
            metadata=json.dumps({"absorbed": absorbed_friendly_ids}),
            comment="",
        )

        winner.stored_event_count = Event.objects.filter(issue_id=winner_id).count()
        max_do = Event.objects.filter(issue_id=winner_id).aggregate(m=Max("digest_order"))["m"] or 0
        if winner.digested_event_count < max_do:
            winner.digested_event_count = max_do
        bounds = Event.objects.filter(issue_id=winner_id).aggregate(
            first=Min("ingested_at"),
            last=Max("ingested_at"),
        )
        if bounds["first"]:
            winner.first_seen = bounds["first"]
        if bounds["last"]:
            winner.last_seen = bounds["last"]
        winner.save(update_fields=["digested_event_count", "stored_event_count", "first_seen", "last_seen"])

        pairs = [(issue.id, issue.project_id) for issue in losers]
        mark_issues_for_deletion(pairs)
        for issue_id, project_id in pairs:
            delay_on_commit(delete_issue_deps, str(project_id), str(issue_id))

        delay_on_commit(send_merge_alert, str(winner_id), absorbed_friendly_ids)


@shared_task
def merge_issues(winner_id, loser_ids, user_id=None, absorbed_friendly_ids=None):
    absorbed_friendly_ids = absorbed_friendly_ids or []
    if merge_issues_batch(winner_id, loser_ids):
        delay_on_commit(merge_issues, winner_id, loser_ids, user_id, absorbed_friendly_ids)
        return
    finalize_merge(winner_id, loser_ids, user_id, absorbed_friendly_ids)

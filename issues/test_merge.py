from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse

from bugsink.test_utils import TransactionTestCase25251 as TransactionTestCase
from events.factories import create_event, create_event_data
from events.models import Event
from issues.factories import get_or_create_issue
from issues.merge import kickoff_merge, merge_issues, validate_merge
from issues.models import Issue, TurningPoint, TurningPointKind
from projects.models import Project, ProjectMembership

User = get_user_model()


class MergeValidationTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(name="merge-proj")
        self.winner, _ = get_or_create_issue(self.project, create_event_data(exception_type="WinnerError"))
        self.loser, _ = get_or_create_issue(self.project, create_event_data(exception_type="LoserError"))

    def test_requires_at_least_one_loser(self):
        with self.assertRaises(AssertionError):
            validate_merge(self.winner, [])

    def test_rejects_cross_project(self):
        other = Project.objects.create(name="other")
        foreign, _ = get_or_create_issue(other, create_event_data(exception_type="ForeignError"))
        with self.assertRaises(AssertionError):
            validate_merge(self.winner, [foreign])

    def test_rejects_winner_in_losers(self):
        with self.assertRaises(AssertionError):
            validate_merge(self.winner, [self.winner, self.loser])


class MergeExecutionTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(name="merge-proj", issue_count=2)
        self.user = User.objects.create_user(username="merger", password="test")
        self.winner, _ = get_or_create_issue(self.project, create_event_data(exception_type="WinnerError"))
        self.loser, _ = get_or_create_issue(self.project, create_event_data(exception_type="LoserError"))
        self.winner_event = create_event(self.project, self.winner, project_digest_order=1)
        self.loser_event = create_event(self.project, self.loser, project_digest_order=2)
        self.loser_grouping = self.loser.grouping_set.get()
        self.loser_friendly = self.loser.friendly_id()

    @patch("alerts.tasks.send_merge_alert")
    def test_merge_moves_events_groupings_and_soft_deletes_losers(self, send_merge_alert):
        kickoff_merge(self.winner, [self.loser], user=self.user)

        self.loser_event.refresh_from_db()
        self.assertEqual(self.loser_event.issue_id, self.winner.id)

        self.loser_grouping.refresh_from_db()
        self.assertEqual(self.loser_grouping.issue_id, self.winner.id)

        # Soft-delete + eager delete_issue_deps removes the loser row entirely in tests.
        self.assertFalse(Issue.objects.filter(id=self.loser.id).exists())

        self.winner.refresh_from_db()
        self.assertEqual(Event.objects.filter(issue_id=self.winner.id).count(), 2)
        self.assertGreaterEqual(self.winner.stored_event_count, 2)

        tp = TurningPoint.objects.filter(issue=self.winner, kind=TurningPointKind.MERGED).get()
        self.assertEqual(tp.user_id, self.user.id)
        self.assertEqual(tp.parsed_metadata()["absorbed"], [self.loser_friendly])

        send_merge_alert.delay.assert_called()
        args, _kwargs = send_merge_alert.delay.call_args
        self.assertEqual(args[0], str(self.winner.id))
        self.assertEqual(args[1], [self.loser_friendly])

    @patch("alerts.tasks.send_merge_alert")
    def test_merge_issues_task_is_idempotent_when_losers_already_gone(self, send_merge_alert):
        merge_issues(str(self.winner.id), [str(self.loser.id)], self.user.id, [self.loser_friendly])
        send_merge_alert.delay.reset_mock()
        # Loser already deleted — finalize should no-op cleanly.
        merge_issues(str(self.winner.id), [str(self.loser.id)], self.user.id, [self.loser_friendly])
        send_merge_alert.delay.assert_not_called()


class MergeViewTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="test", password="test")
        self.project = Project.objects.create(name="test")
        ProjectMembership.objects.create(project=self.project, user=self.user, accepted=True)
        self.winner, _ = get_or_create_issue(self.project, create_event_data(exception_type="WinnerError"))
        self.loser, _ = get_or_create_issue(self.project, create_event_data(exception_type="LoserError"))
        create_event(self.project, self.winner, project_digest_order=1)
        create_event(self.project, self.loser, project_digest_order=2)
        self.client.force_login(self.user)

    def test_merge_button_shown_on_project_issue_list(self):
        response = self.client.get(f"/issues/{self.project.id}/")
        self.assertContains(response, 'name="action" value="merge"')
        self.assertContains(response, "Merge…")

    def test_merge_action_shows_winner_picker(self):
        response = self.client.post(
            f"/issues/{self.project.id}/",
            {
                "issue_ids[]": [str(self.winner.id), str(self.loser.id)],
                "action": "merge",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Merge into selected")
        self.assertContains(response, self.winner.friendly_id())
        self.assertContains(response, self.loser.friendly_id())

    def test_merge_requires_two_issues(self):
        response = self.client.post(
            f"/issues/{self.project.id}/",
            {"issue_ids[]": [str(self.winner.id)], "action": "merge"},
            follow=True,
        )
        self.assertContains(response, "Select at least two issues to merge")

    @patch("issues.merge.merge_issues")
    def test_confirm_merge_kicks_off_task(self, merge_issues_task):
        response = self.client.post(
            reverse("issue_merge", kwargs={"project_pk": self.project.pk}),
            {
                "action": "confirm_merge",
                "winner_id": str(self.winner.id),
                "issue_ids[]": [str(self.winner.id), str(self.loser.id)],
            },
            follow=True,
        )
        self.assertContains(response, "Merging")
        merge_issues_task.delay.assert_called()
        args, _kwargs = merge_issues_task.delay.call_args
        self.assertEqual(args[0], str(self.winner.id))
        self.assertEqual(set(args[1]), {str(self.loser.id)})

import datetime
import json

from django.utils import timezone
from rest_framework import serializers

from bugsink.api_serializers import UTCModelSerializer
from bugsink.period_utils import DATEUTIL_KWARGS_MAP

from .models import ExternalIssue, Issue, TurningPoint, TurningPointKind, issue_lookup_kwargs


class ExternalIssueSerializer(UTCModelSerializer):
    """Sentry-shaped external issue: webUrl / project / identifier (+ provider, metadata)."""

    webUrl = serializers.URLField(source="web_url")
    project = serializers.CharField(source="external_project", allow_blank=True, required=False, default="")
    displayName = serializers.CharField(source="display_name", allow_blank=True, required=False, default="")
    metadata = serializers.JSONField(required=False, default=dict)
    issue = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = ExternalIssue
        fields = [
            "id",
            "issue",
            "provider",
            "webUrl",
            "project",
            "identifier",
            "displayName",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["metadata"] = instance.parsed_metadata()
        data["displayName"] = instance.get_display_name()
        data["issue"] = str(instance.issue_id)
        return data

    def validate_metadata(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be a JSON object.")
        return value

    def _resolve_issue(self, issue_ref):
        if isinstance(issue_ref, Issue):
            return issue_ref
        try:
            return Issue.objects.filter(is_deleted=False).select_related("project").get(
                **issue_lookup_kwargs(str(issue_ref)))
        except Issue.DoesNotExist as exc:
            raise serializers.ValidationError({"issue": "Issue not found."}) from exc

    def create(self, validated_data):
        issue_ref = validated_data.pop("issue", None)
        if issue_ref is None:
            raise serializers.ValidationError({"issue": "This field is required."})
        issue = self._resolve_issue(issue_ref)
        metadata = validated_data.pop("metadata", {})
        return ExternalIssue.objects.create(
            project=issue.project,
            issue=issue,
            metadata=json.dumps(metadata),
            **validated_data,
        )

    def update(self, instance, validated_data):
        validated_data.pop("issue", None)
        metadata = validated_data.pop("metadata", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if metadata is not None:
            instance.metadata = json.dumps(metadata)
            if hasattr(instance, "_parsed_metadata"):
                del instance._parsed_metadata
        instance.save()
        return instance


class IssueMergeSerializer(serializers.Serializer):
    """Sentry-like merge body: children absorbed into the path issue (parent)."""

    children = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=False,
        help_text="Issue UUIDs or friendly IDs to absorb into the parent.",
    )


class IssueMetadataSerializer(serializers.Serializer):
    metadata = serializers.JSONField()
    merge = serializers.BooleanField(default=True, required=False)

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be a JSON object.")
        return value


class IssueSerializer(UTCModelSerializer):
    # grouping_keys = serializers.SerializerMethodField()  # read-only list of strings
    friendly_id = serializers.CharField(read_only=True)
    metadata = serializers.SerializerMethodField()
    external_issues = ExternalIssueSerializer(many=True, read_only=True)

    class Meta:
        model = Issue

        # TODO better wording:
        # This is the first attempt at getting the list of fields right. My belief is: this is a nice minimal list.
        # it _does_ contain `data`, which is typically quite "fat", but I'd say that's the most useful field to have.
        # and when you're actually in the business of looking at a specific event, you want to see the data.
        fields = [
            "id",
            "friendly_id",
            "project",
            "digest_order",
            "last_seen",
            "first_seen",
            "digested_event_count",
            "stored_event_count",
            "calculated_type",
            "calculated_value",
            "transaction",
            # "last_frame_filename",
            # "last_frame_module",
            # "last_frame_function",
            "is_resolved",
            "is_resolved_unconditionally",
            "is_resolved_by_next_release",
            # "fixed_at",  too "raw"? i.e. too implementation-tied?
            # "events_at",  too "raw"? i.e. too implementation-tied?
            "is_muted",
            # "unmute_on_volume_based_conditions",  too "raw"? i.e. too implementation-tied?
            # "grouping_keys",  TODO (likely) once we have the "expand" idea implemented
            "metadata",
            "external_issues",
        ]

    def get_metadata(self, obj):
        return obj.parsed_metadata()


class IssueMuteForSerializer(serializers.Serializer):
    period_name = serializers.ChoiceField(choices=tuple(DATEUTIL_KWARGS_MAP.keys()))
    nr_of_periods = serializers.IntegerField(min_value=1)


class IssueMuteUntilSerializer(IssueMuteForSerializer):
    gte_threshold = serializers.IntegerField(min_value=1)


class IssueField(serializers.CharField):
    def to_internal_value(self, value):
        value = super().to_internal_value(value)
        try:
            return Issue.objects.filter(is_deleted=False).select_related("project").get(**issue_lookup_kwargs(value))
        except Issue.DoesNotExist:
            raise serializers.ValidationError("Issue not found.")

    def to_representation(self, issue):
        return str(issue.id)  # JSON wants strings, not UUIDs.


class IssueCommentSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    issue = IssueField()
    project = serializers.IntegerField(source="project_id", read_only=True)
    timestamp = serializers.DateTimeField(read_only=True, default_timezone=datetime.timezone.utc)
    comment = serializers.CharField(allow_blank=False, trim_whitespace=True)
    user = serializers.IntegerField(source="user_id", read_only=True, allow_null=True)

    def create(self, validated_data):
        issue = validated_data["issue"]
        return TurningPoint.objects.create(
            project=issue.project,
            issue=issue,
            kind=TurningPointKind.MANUAL_ANNOTATION,
            user=None,  # Bearer-token API auth currently represents a global token, not a user.
            comment=validated_data["comment"],
            timestamp=timezone.now(),
        )

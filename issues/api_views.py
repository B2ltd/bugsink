from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import CursorPagination
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes

from bugsink.api_mixins import AtomicRequestMixin
from bugsink.utils import assert_

from .merge import kickoff_merge
from .models import ExternalIssue, Issue, IssueStateManager, TurningPoint, apply_issue_action, issue_lookup_kwargs
from .serializers import (
    ExternalIssueSerializer,
    IssueCommentSerializer,
    IssueMergeSerializer,
    IssueMetadataSerializer,
    IssueMuteForSerializer,
    IssueMuteUntilSerializer,
    IssueNotifySerializer,
    IssueSerializer,
)


class IssuesCursorPagination(CursorPagination):
    """
    Cursor paginator for /issues supporting ?sort=… and ?order=asc|desc.

    Sort modes are named after the *primary* column:
      - sort=digest_order         → unique per project → no tie-breakers needed
      - sort=last_seen            → timestamp          → tie-breaker on id
      - sort=digested_event_count → lifetime count     → tie-breakers on last_seen and id

    Direction applies to primary *and beyond* (i.e. all fields in the list).
    The view MUST filter by project; ordering is handled here.
    """
    # Cursor pagination requires an indexed, mostly-stable ordering. Stable mode: sort=digest_order (default). We
    # require ?project=<uuid> and have a composite (project_id, digest_order) index, so ORDER BY digest_order after
    # filtering by project is fast and cursor-stable.

    # We also offer a "recent" mode: sort=last_seen. This is not stable, as new events can come in mid-cursor, and
    # reshuffle things causing misses or duplicates. However, this is the desired UX for a "recent activity" view.
    # i.e. the typical usage would in fact just be to get the "first page" of recent activity.
    # Event-count sorting has the same instability when new events arrive.
    page_size = 250
    default_direction = "asc"
    default_sort = "digest_order"

    VALID_SORTS = ("digest_order", "last_seen", "digested_event_count")
    VALID_ORDERS = ("asc", "desc")

    def get_ordering(self, request, queryset, view):
        sort = request.query_params.get("sort", self.default_sort)
        if sort not in self.VALID_SORTS:
            raise ValidationError({
                "sort": ["Must be 'digest_order', 'last_seen', or 'digested_event_count'."],
            })

        order = request.query_params.get("order", self.default_direction)
        if order not in self.VALID_ORDERS:
            raise ValidationError({"order": ["Must be 'asc' or 'desc'."]})

        desc = (order == "desc")

        if sort == "digest_order":
            # Unique per project; stable cursor once filtered by project.
            return ["-digest_order" if desc else "digest_order"]

        fields = ["last_seen", "id"] if sort == "last_seen" else ["digested_event_count", "last_seen", "id"]
        return [f"-{field}" for field in fields] if desc else fields


class IssueViewSet(AtomicRequestMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Issue.objects.filter(is_deleted=False).select_related("project").prefetch_related("external_issues")
    serializer_class = IssueSerializer
    pagination_class = IssuesCursorPagination
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return self.queryset

    @extend_schema(
        summary="List issues",
        description="List issues for a project.",
        parameters=[
            OpenApiParameter(
                name="project",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Filter issues by project id (required).",
            ),
            OpenApiParameter(
                name="sort",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=["digest_order", "last_seen", "digested_event_count"],
                description="Sort mode (default: digest_order).",
            ),
            OpenApiParameter(
                name="order",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=["asc", "desc"],
                description="Sort order (default: asc).",
            ),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Retrieve an issue",
        description="Retrieve an issue by issue UUID or friendly ID.",
        responses=IssueSerializer,
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        summary="Delete an issue",
        description="Delete an issue.",
    )
    def destroy(self, request, *args, **kwargs):
        issue = self.get_object()
        issue.delete_deferred()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if self.action != "list":
            return queryset

        project = self.request.query_params.get("project")
        if not project:
            # the below at least until we have a UI for cross-project Issue listing, i.e. #190
            raise ValidationError({"project": ["This field is required."]})

        return queryset.filter(project=project)

    def get_object(self):
        """
        DRF's get_object(), but bypass filter_queryset for detail.
        """
        # NOTE: alternatively, we just complain hard when a filter is applied to a detail view.
        # TODO: copy/paste from events/api_views.py
        queryset = self.get_queryset()

        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        assert_(
            lookup_url_kwarg in self.kwargs,
            'Expected view %s to be called with a URL keyword argument named "%s".'
            % (self.__class__.__name__, lookup_url_kwarg)
        )

        obj = get_object_or_404(queryset, **issue_lookup_kwargs(self.kwargs[lookup_url_kwarg]))
        self.check_object_permissions(self.request, obj)
        return obj

    def _action_response(self, issue):
        issue.save()
        return Response(self.get_serializer(issue).data)

    def _assert_unresolved(self, issue):
        if issue.is_resolved:
            raise ValidationError({"detail": "Issue is already resolved."})

    def _assert_resolved(self, issue):
        if not issue.is_resolved:
            raise ValidationError({"detail": "Issue is not resolved."})

    def _assert_unmuted(self, issue):
        if issue.is_muted:
            raise ValidationError({"detail": "Issue is already muted."})

    def _apply_issue_action(self, issue, action):
        # Bearer-token API auth currently represents a global token, not a user.
        apply_issue_action(IssueStateManager, issue, action, user=None)
        return self._action_response(issue)

    @extend_schema(
        summary="Resolve an issue",
        description="Mark this issue as resolved.",
        request=OpenApiTypes.NONE,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        issue = self.get_object()
        self._assert_unresolved(issue)
        return self._apply_issue_action(issue, "resolve")

    @extend_schema(
        summary="Resolve an issue in the next release",
        description="Mark this issue as resolved by the next release.",
        request=OpenApiTypes.NONE,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["post"], url_path="resolve-next")
    def resolve_next(self, request, pk=None):
        issue = self.get_object()
        self._assert_unresolved(issue)
        return self._apply_issue_action(issue, "resolved_next")

    @extend_schema(
        summary="Resolve an issue in the latest release",
        description="Mark this issue as resolved in the latest release.",
        request=OpenApiTypes.NONE,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["post"], url_path="resolve-latest")
    def resolve_latest(self, request, pk=None):
        issue = self.get_object()
        self._assert_unresolved(issue)
        if not issue.project.has_releases:
            raise ValidationError({"detail": "Project has no releases."})

        latest_release = issue.project.get_latest_release()
        return self._apply_issue_action(issue, "resolved_release:" + latest_release.version)

    @extend_schema(
        summary="Reopen an issue",
        description="Mark this resolved issue as unresolved again.",
        request=OpenApiTypes.NONE,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        issue = self.get_object()
        self._assert_resolved(issue)
        return self._apply_issue_action(issue, "reopen")

    @extend_schema(
        summary="Mute an issue",
        description="Mute this issue.",
        request=OpenApiTypes.NONE,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["post"])
    def mute(self, request, pk=None):
        issue = self.get_object()
        self._assert_unresolved(issue)
        self._assert_unmuted(issue)
        return self._apply_issue_action(issue, "mute")

    @extend_schema(
        summary="Mute an issue for a period",
        description="Mute this issue for a relative period, e.g. for 3 days.",
        request=IssueMuteForSerializer,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["post"], url_path="mute-for")
    def mute_for(self, request, pk=None):
        serializer = IssueMuteForSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        period_name = serializer.validated_data["period_name"]
        nr_of_periods = serializer.validated_data["nr_of_periods"]

        issue = self.get_object()
        self._assert_unresolved(issue)
        self._assert_unmuted(issue)
        return self._apply_issue_action(issue, f"mute_for:{period_name},{nr_of_periods},")

    @extend_schema(
        summary="Mute an issue until a threshold is reached",
        description="Mute this issue until a threshold is reached, e.g. more than 10 events in 1 hour.",
        request=IssueMuteUntilSerializer,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["post"], url_path="mute-until")
    def mute_until(self, request, pk=None):
        serializer = IssueMuteUntilSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        period_name = serializer.validated_data["period_name"]
        nr_of_periods = serializer.validated_data["nr_of_periods"]
        gte_threshold = serializer.validated_data["gte_threshold"]

        issue = self.get_object()
        self._assert_unresolved(issue)
        self._assert_unmuted(issue)
        return self._apply_issue_action(issue, f"mute_until:{period_name},{nr_of_periods},{gte_threshold}")

    @extend_schema(
        summary="Unmute an issue",
        description="Unmute this issue.",
        request=OpenApiTypes.NONE,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["post"])
    def unmute(self, request, pk=None):
        issue = self.get_object()
        self._assert_unresolved(issue)
        if not issue.is_muted:
            raise ValidationError({"detail": "Issue is not muted."})

        return self._apply_issue_action(issue, "unmute")

    @extend_schema(
        summary="Merge issues into this issue",
        description=(
            "Absorb `children` into this issue (the parent). Same idea as Sentry's merge: "
            "future events matching any absorbed grouping land on the parent."
        ),
        request=IssueMergeSerializer,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["post"])
    def merge(self, request, pk=None):
        parent = self.get_object()
        serializer = IssueMergeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        children = []
        for child_ref in serializer.validated_data["children"]:
            try:
                child = Issue.objects.filter(is_deleted=False).get(**issue_lookup_kwargs(child_ref))
            except Issue.DoesNotExist as exc:
                raise ValidationError({"children": [f"Issue not found: {child_ref}"]}) from exc
            children.append(child)

        try:
            kickoff_merge(parent, children, user=None)
        except AssertionError as exc:
            raise ValidationError({"detail": str(exc) or "Invalid merge."}) from exc

        parent.refresh_from_db()
        data = self.get_serializer(parent).data
        data["merge"] = {
            "parent": str(parent.id),
            "children": [str(child.id) for child in children],
        }
        return Response(data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="Notify messaging services for an issue",
        description=(
            "Queue alert(s) for this issue. With `service_id`, that service is always used "
            "(ignores alert_on_* flags). Without it, every project service that accepts "
            "`alert_reason` is used. Default reason is MERGED so the GitHub Issues webhook "
            "creates a Bug when none is linked yet."
        ),
        request=IssueNotifySerializer,
        responses={202: OpenApiTypes.OBJECT},
    )
    @action(detail=True, methods=["post"])
    def notify(self, request, pk=None):
        from alerts.models import MessagingServiceConfig
        from alerts.tasks import send_manual_service_alert

        issue = self.get_object()
        serializer = IssueNotifySerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        alert_reason = serializer.validated_data.get("alert_reason") or "MERGED"
        service_id = serializer.validated_data.get("service_id")

        if service_id is not None:
            try:
                services = [issue.project.service_configs.get(pk=service_id)]
            except MessagingServiceConfig.DoesNotExist as exc:
                raise ValidationError({"service_id": ["Unknown messaging service for this project."]}) from exc
        else:
            services = [
                s for s in issue.project.service_configs.all()
                if s.should_send_alert(alert_reason)
            ]
            if not services:
                raise ValidationError({
                    "detail": f"No messaging services accept alert_reason={alert_reason}.",
                })

        queued = []
        for service in services:
            send_manual_service_alert.delay(str(issue.id), service.id, alert_reason)
            queued.append({"service_id": service.id, "display_name": service.display_name, "kind": service.kind})

        return Response(
            {
                "issue": str(issue.id),
                "alert_reason": alert_reason,
                "queued": queued,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Update issue metadata",
        description="Shallow-merge (default) or replace free-form JSON metadata on this issue.",
        request=IssueMetadataSerializer,
        responses=IssueSerializer,
    )
    @action(detail=True, methods=["patch"], url_path="metadata")
    def update_metadata(self, request, pk=None):
        issue = self.get_object()
        serializer = IssueMetadataSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        issue.set_metadata(
            serializer.validated_data["metadata"],
            merge=serializer.validated_data.get("merge", True),
        )
        issue.save(update_fields=["metadata"])
        return self._action_response(issue)


class IssueExternalIssueViewSet(
    AtomicRequestMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """CRUD for Sentry-shaped external issue links (GitHub URL, etc.)."""

    queryset = ExternalIssue.objects.select_related("issue", "project")
    serializer_class = ExternalIssueSerializer
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = self.queryset
        issue_ref = self.request.query_params.get("issue")
        if self.action == "list":
            if not issue_ref:
                raise ValidationError({"issue": ["This field is required."]})
            try:
                issue = Issue.objects.filter(is_deleted=False).get(**issue_lookup_kwargs(issue_ref))
            except Issue.DoesNotExist as exc:
                raise ValidationError({"issue": ["Issue not found."]}) from exc
            return qs.filter(issue=issue)
        return qs

    @extend_schema(
        summary="List external issues",
        parameters=[
            OpenApiParameter(
                name="issue",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Issue UUID or friendly ID.",
            ),
        ],
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def perform_destroy(self, instance):
        instance.delete()


class IssueCommentViewSet(AtomicRequestMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    queryset = TurningPoint.objects.none()  # router basename only
    serializer_class = IssueCommentSerializer
    http_method_names = ["post", "head", "options"]

    @extend_schema(
        summary="Create an issue comment",
        description="Add a comment to an issue. `issue` accepts an issue UUID or friendly ID.",
        request=IssueCommentSerializer,
        responses=IssueCommentSerializer,
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

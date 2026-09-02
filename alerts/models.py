from django.db import models
from projects.models import Project

from .service_backends.slack import SlackBackend
from .service_backends.mattermost import MattermostBackend
from .service_backends.discord import DiscordBackend
from .service_backends.telegram import TelegramBackend
from .service_backends.custom import CustomBackend
from .service_backends.msteams import MsTeamsBackend


def get_alert_service_kind_choices():
    # As a callable to avoid non-DB-affecting migrations for adding new kinds.
    # Messaging backends don't need translations since they are brand names.
    return [
        ("discord", "Discord"),
        ("mattermost", "Mattermost"),
        ("msteams", "Microsoft Teams"),
        ("slack", "Slack"),
        ("telegram", "Telegram"),
        ("custom", "Custom"),
    ]


def get_alert_service_backend_class(kind):
    if kind == "discord":
        return DiscordBackend
    if kind == "mattermost":
        return MattermostBackend
    if kind == "msteams":
        return MsTeamsBackend
    if kind == "slack":
        return SlackBackend
    if kind == "telegram":
        return TelegramBackend
    if kind == "custom":
        return CustomBackend
    raise ValueError(f"Unknown backend kind: {kind}")


class MessagingServiceConfig(models.Model):
    project = models.ForeignKey(Project, on_delete=models.DO_NOTHING, related_name="service_configs")
    display_name = models.CharField(max_length=100, blank=False,
                                    help_text='For display in the UI, e.g. "#general on company Slack"')

    kind = models.CharField(choices=get_alert_service_kind_choices, max_length=20, default="slack")

    config = models.TextField(blank=False)

    # Which alert reasons this destination should receive. Project-level alert_on_* remain outer kill-switches.
    alert_on_new = models.BooleanField(default=True, help_text="Send when a new issue is first seen")
    alert_on_regression = models.BooleanField(default=True, help_text="Send when a resolved issue regresses")
    alert_on_unmute = models.BooleanField(
        default=True, help_text="Send when a muted issue is unmuted by volume/time")
    alert_on_merge = models.BooleanField(
        default=False,
        help_text="Send when issues are manually merged (useful to notify GitHub only after triage)")

    # Alert backend failure tracking
    last_failure_timestamp = models.DateTimeField(null=True, blank=True,
                                                  help_text="When the last failure occurred")
    last_failure_status_code = models.IntegerField(null=True, blank=True,
                                                   help_text="HTTP status code of the failed request")
    last_failure_response_text = models.TextField(null=True, blank=True,
                                                  help_text="Response text from the failed request")
    last_failure_is_json = models.BooleanField(null=True, blank=True,
                                               help_text="Whether the response was valid JSON")
    last_failure_error_type = models.CharField(max_length=100, null=True, blank=True,
                                               help_text="Type of error that occurred (e.g., 'requests.HTTPError')")
    last_failure_error_message = models.TextField(null=True, blank=True,
                                                  help_text="Error message from the exception")

    ALERT_REASON_FLAGS = {
        "NEW": "alert_on_new",
        "REGRESSED": "alert_on_regression",
        "UNMUTED": "alert_on_unmute",
        "MERGED": "alert_on_merge",
    }

    def should_send_alert(self, alert_reason):
        flag = self.ALERT_REASON_FLAGS.get(alert_reason)
        if flag is None:
            return True  # unknown reasons still fire (forward-compatible)
        return bool(getattr(self, flag))

    def get_backend(self):
        return get_alert_service_backend_class(self.kind)(self)

    def clear_failure_status(self):
        """Clear all failure tracking fields on successful operation"""
        self.last_failure_timestamp = None
        self.last_failure_status_code = None
        self.last_failure_response_text = None
        self.last_failure_is_json = None
        self.last_failure_error_type = None
        self.last_failure_error_message = None

    def has_recent_failure(self):
        """Check if this config has a recent failure"""
        return self.last_failure_timestamp is not None

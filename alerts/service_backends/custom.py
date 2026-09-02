import json
import re

import requests
from django import forms
from django.utils import timezone

from bugsink.app_settings import get_settings
from bugsink.transaction import immediate_atomic
from issues.models import Issue
from issues.serializers import IssueSerializer
from snappea.decorators import shared_task

from .base import BaseWebhookBackend
from .webhook_security import validate_webhook_url

# Headers that safe_post / the pinned adapter own; callers must not set them.
_RESERVED_HEADER_NAMES = frozenset({"host"})

_HEADER_LINE_RE = re.compile(r"^([^:\s][^:]*):\s*(.*)$")


def parse_extra_headers(raw: str) -> dict[str, str]:
    """Parse 'Name: Value' lines into a header dict. Blank lines and '# …' comments are ignored."""
    if not raw or not raw.strip():
        return {}

    headers: dict[str, str] = {}
    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _HEADER_LINE_RE.match(stripped)
        if not match:
            raise ValueError(f"Invalid header on line {line_no} (expected 'Name: Value'): {stripped!r}")
        name, value = match.group(1).strip(), match.group(2).strip()
        if name.lower() in _RESERVED_HEADER_NAMES:
            raise ValueError(f"Header {name!r} is reserved and cannot be set")
        if not name:
            raise ValueError(f"Empty header name on line {line_no}")
        headers[name] = value
    return headers


def build_request_headers(extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        # Caller-supplied headers win except we always send JSON unless they override Content-Type.
        headers.update(extra_headers)
    return headers


class CustomBackendForm(forms.Form):
    webhook_url = forms.URLField(required=True, assume_scheme="https")
    extra_headers = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Authorization: Bearer …\nX-Env: uk"}),
        help_text=(
            "Optional. One header per line as <code>Name: Value</code>. "
            "Use for bearer tokens / shared secrets. <code>Host</code> is reserved."
        ),
    )

    def __init__(self, *args, **kwargs):
        config = kwargs.pop("config", None)

        super().__init__(*args, **kwargs)
        if config:
            self.fields["webhook_url"].initial = config.get("webhook_url", "")
            self.fields["extra_headers"].initial = config.get("extra_headers", "")

    def get_config(self):
        return {
            "webhook_url": self.cleaned_data.get("webhook_url"),
            "extra_headers": self.cleaned_data.get("extra_headers") or "",
        }

    def clean_webhook_url(self):
        webhook_url = self.cleaned_data["webhook_url"]
        try:
            validate_webhook_url(webhook_url)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e
        return webhook_url

    def clean_extra_headers(self):
        raw = self.cleaned_data.get("extra_headers") or ""
        try:
            parse_extra_headers(raw)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e
        return raw


def _store_failure_info(service_config_id, exception, response=None):
    """Store failure information in the MessagingServiceConfig with immediate_atomic"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)

            config.last_failure_timestamp = timezone.now()
            config.last_failure_error_type = type(exception).__name__
            config.last_failure_error_message = str(exception)

            # Handle requests-specific errors
            if response is not None:
                config.last_failure_status_code = response.status_code
                config.last_failure_response_text = response.text[:2000]  # Limit response text size

                # Check if response is JSON
                try:
                    json.loads(response.text)
                    config.last_failure_is_json = True
                except (json.JSONDecodeError, ValueError):
                    config.last_failure_is_json = False
            else:
                # Non-HTTP errors
                config.last_failure_status_code = None
                config.last_failure_response_text = None
                config.last_failure_is_json = None

            config.save()
        except MessagingServiceConfig.DoesNotExist:
            # Config was deleted while task was running
            pass


def _store_success_info(service_config_id):
    """Clear failure information on successful operation"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)
            config.clear_failure_status()
            config.save()
        except MessagingServiceConfig.DoesNotExist:
            # Config was deleted while task was running
            pass


def _headers_from_config(extra_headers_raw: str) -> dict[str, str]:
    try:
        return build_request_headers(parse_extra_headers(extra_headers_raw or ""))
    except ValueError:
        # Config was valid when saved; if somehow corrupted, still send Content-Type only.
        return build_request_headers()


@shared_task
def custom_backend_send_test_message(
    webhook_url, project_name, display_name, service_config_id, extra_headers=""
):
    data = {
        "id": "497f6eca-6276-4993-bfeb-53cbbbba6f08",
        "friendly_id": "TEST-1",
        "project": 1,
        "digest_order": 1,
        "first_seen": "2024-01-10T08:15:00Z",
        "last_seen": "2024-01-15T10:30:00Z",
        "digested_event_count": 15,
        "stored_event_count": 15,
        "calculated_type": "ValueError",
        "calculated_value": "invalid literal for int()",
        "transaction": "/api/users/login",
        "is_resolved": False,
        "is_resolved_unconditionally": False,
        "is_resolved_by_next_release": False,
        "is_muted": False,
        "title": "ValueError: invalid literal for int()",
        "project_name": project_name,
        "url": "https://bugsink.example.com/issues/497f6eca-6276-4993-bfeb-53cbbbba6f08/",
        "alert_reason": "TEST",
    }

    try:
        result = CustomBackend.safe_post(
            webhook_url,
            data=json.dumps(data),
            headers=_headers_from_config(extra_headers),
        )

        result.raise_for_status()

        _store_success_info(service_config_id)
    except requests.RequestException as e:
        response = getattr(e, "response", None)
        _store_failure_info(service_config_id, e, response)

    except Exception as e:
        _store_failure_info(service_config_id, e)


@shared_task
def custom_backend_send_alert(
    webhook_url,
    issue_id,
    state_description,
    alert_article,
    alert_reason,
    service_config_id,
    unmute_reason=None,
    extra_headers="",
):
    issue = Issue.objects.get(id=issue_id)

    # Deliberately mirror the canonical issue API; a test keeps the test-message fields aligned with this payload.
    data = dict(IssueSerializer(issue).data)

    # Add additional convenience fields
    data["title"] = issue.title()
    data["project_name"] = issue.project.name
    data["url"] = get_settings().BASE_URL + issue.get_absolute_url()
    data["alert_reason"] = alert_reason

    if unmute_reason:
        data["unmute_reason"] = unmute_reason

    try:
        result = CustomBackend.safe_post(
            webhook_url,
            data=json.dumps(data),
            headers=_headers_from_config(extra_headers),
        )

        result.raise_for_status()

        _store_success_info(service_config_id)
    except requests.RequestException as e:
        response = getattr(e, "response", None)
        _store_failure_info(service_config_id, e, response)

    except Exception as e:
        _store_failure_info(service_config_id, e)


class CustomBackend(BaseWebhookBackend):
    def __init__(self, service_config):
        self.service_config = service_config

    @classmethod
    def get_form_class(cls):
        return CustomBackendForm

    def send_test_message(self):
        config = json.loads(self.service_config.config)
        custom_backend_send_test_message.delay(
            config["webhook_url"],
            self.service_config.project.name,
            self.service_config.display_name,
            self.service_config.id,
            config.get("extra_headers", ""),
        )

    def send_alert(self, issue_id, state_description, alert_article, alert_reason, **kwargs):
        config = json.loads(self.service_config.config)
        custom_backend_send_alert.delay(
            config["webhook_url"],
            issue_id,
            state_description,
            alert_article,
            alert_reason,
            self.service_config.id,
            extra_headers=config.get("extra_headers", ""),
            **kwargs,
        )

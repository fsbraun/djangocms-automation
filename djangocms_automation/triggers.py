"""Trigger registry and base classes for automation triggers.

Provides an abstract base ``Trigger`` class and a global ``trigger_registry``
for registering concrete triggers that can be referenced by slug/id in
Automations. Each trigger defines:
- id: machine identifier (unique key)
- name: human readable name
- description: short explanatory text
- data_schema: JSON schema (Draft 2020-12 compatible subset) describing
  the expected structure of the trigger data payload.

Concrete example triggers are provided for "Click" and "Mail" events.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any, Callable

from django import forms
from django.apps import apps
from django.contrib.admin import widgets
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from .utilities.json import cleaned_data_to_json_serializable

try:  # Optional dependency, added to pyproject but keep graceful fallback
    from jsonschema import Draft202012Validator, ValidationError
except ImportError:  # pragma: no cover - fallback if not installed
    Draft202012Validator = None  # type: ignore
    ValidationError = Exception  # type: ignore


class Trigger(forms.Form):
    """Abstract trigger definition.

    Instances of subclasses are *definitions* (metadata), not runtime events.
    The ``data_schema`` should be a minimal JSON schema dict that downstream
    code can use for validation (e.g. with ``jsonschema``) when data for a
    trigger is supplied.

    Configuration fields can be defined as regular Django form fields on the
    trigger class. These will be automatically injected into the admin form
    and their values stored in the config JSON field.
    """

    id: str
    name: str
    description: str
    icon: str
    data_schema: dict[str, Any] = {}

    def validate_payload(
        self,
        payload: dict[str, Any],
        validator: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
        raise_errors: bool = True,
    ) -> bool:
        """Validate a payload against this trigger's ``data_schema``.

        If ``jsonschema`` is available it will perform full schema validation.
        Otherwise it falls back to a shallow required-field presence check.

        Parameters
        ----------
        payload: dict
            The candidate data.
        validator: callable | None
            Custom validator function (schema, payload) -> None (raise on error).
        raise_errors: bool
            If True, raise ValidationError/ValueError on failure; else return False.

        Returns
        -------
        bool
            True if validation successful, otherwise False (in non-raising mode).
        """
        schema = self.data_schema or {}
        if not schema:
            return True  # No schema to validate against

        try:
            if validator is not None:
                validator(schema, payload)
                return True
            if Draft202012Validator is not None:
                Draft202012Validator(schema).validate(payload)
                return True
            # Fallback shallow check
            required = schema.get("required", [])
            ok = all(r in payload for r in required)
            if not ok and raise_errors:
                missing = [r for r in required if r not in payload]
                raise ValueError(f"Missing required fields for trigger '{self.id}': {missing}")
            return ok
        except ValidationError:
            if raise_errors:
                raise
            return False
        except ValueError:
            if raise_errors:
                raise
            return False


class TriggerRegistry:
    """Registry for available trigger definitions."""

    def __init__(self):
        self._triggers: dict[str, Trigger] = {}

    def register(self, trigger: Trigger):
        self._triggers[trigger.id] = trigger

    def unregister(self, trigger_id: str):
        self._triggers.pop(trigger_id, None)

    def get(self, trigger_id: str) -> Trigger | None:
        return self._triggers.get(trigger_id)

    def all(self) -> list[Trigger]:
        return list(self._triggers.values())

    def get_choices(self) -> list[tuple[str, str]]:
        return [(t.id, t.name) for t in self._triggers.values()]


# Global registry instance
trigger_registry = TriggerRegistry()

# ---------------------------------------------------------------------------
# Webhook framework
# ---------------------------------------------------------------------------


def generate_webhook_token() -> str:
    """Generate a high-entropy URL-safe webhook token."""
    return secrets.token_urlsafe(24)


class WebhookTrigger(Trigger):
    """Base class for triggers fired by an inbound HTTP webhook.

    Each trigger of a webhook type gets a secret ``token`` in its config;
    POSTing to ``.../webhook/<token>/`` (see ``djangocms_automation.urls``)
    fires the trigger. Subclasses customize payload handling:

    - :meth:`parse_payload` — turn the request into data rows. Return an
      empty list to accept-but-ignore the request (e.g. filtered out).
      Raise ``ValueError`` for a malformed payload (results in HTTP 400).
    - :meth:`verify_request` — authenticate the request beyond the URL
      token. The default verifies an optional HMAC-SHA256 signature
      (``signing_secret`` config + ``X-Automation-Signature`` header,
      hex digest of the raw request body).

    Rows are validated against :attr:`data_schema` before triggering.
    """

    signature_header = "X-Automation-Signature"

    # Configuration form fields (stored in the trigger's config JSON)
    token = forms.CharField(
        label=_("Webhook token"),
        required=False,  # auto-generated on save when left empty
        initial=generate_webhook_token,
        help_text=_("Secret token identifying this webhook. The endpoint URL is …/webhook/<token>/."),
    )
    signing_secret = forms.CharField(
        label=_("Signing secret"),
        required=False,
        help_text=_(
            "Optional. If set, requests must include an X-Automation-Signature header containing "
            "the hex HMAC-SHA256 of the raw request body, keyed with this secret."
        ),
    )

    def verify_request(self, request, config: dict[str, Any]) -> bool:
        """Verify the request's authenticity (beyond the URL token).

        :returns: True if the request may fire the trigger.
        """
        secret = (config or {}).get("signing_secret")
        if not secret:
            return True
        provided = request.headers.get(self.signature_header, "")
        expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    def parse_payload(self, request, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse the request body into data rows.

        The default accepts a JSON object (one row) or a JSON array of
        objects (multiple rows).

        :raises ValueError: If the payload is malformed.
        """
        try:
            data = json.loads(request.body.decode() or "null")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Invalid JSON payload: {exc}") from exc
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list) and all(isinstance(row, dict) for row in data):
            return data
        raise ValueError("Expected a JSON object or an array of objects.")


class GenericWebhookTrigger(WebhookTrigger):
    id = "webhook"
    name = _("Webhook")
    description = _("Starts when an HTTP POST request is received on the trigger's webhook URL.")
    icon = "bi-broadcast-pin"
    data_schema = {}


# ---------------------------------------------------------------------------
# Example concrete trigger definitions
# ---------------------------------------------------------------------------


# Click Trigger schema: expects element metadata and optional context
class ClickTrigger(Trigger):
    id = "click"
    name = _("Manual")
    description = _("Starts when a staff user selects the automation to be started")
    icon = "bi-mouse"
    data_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["element_id", "timestamp"],
        "properties": {
            "element_id": {"type": "string", "minLength": 1},
            "timestamp": {"type": "string", "format": "date-time"},
            "path": {"type": "string"},
            "metadata": {"type": "object"},
        },
        "additionalProperties": True,
    }


# Mail Trigger: webhook-based mail ingestion (example WebhookTrigger implementation)
class MailTrigger(WebhookTrigger):
    """Mail ingestion via webhook — the example :class:`WebhookTrigger`.

    Point your mail provider's inbound/event webhook at this trigger's URL.
    The payload is normalized from common provider field aliases (``to`` /
    ``To`` / ``recipient``, ``TextBody`` / ``text`` / ``body_text``, ...)
    into the trigger's data schema, and the configured recipient/subject/
    status filters decide whether the automation actually starts. For a
    provider whose payload differs structurally, subclass and override
    :meth:`normalize_row` (or :meth:`parse_payload`) and register the new
    trigger type.
    """

    id = "mail"
    name = _("Mail")
    description = _("Starts when an email is received or its status updates (via webhook).")
    icon = "bi-envelope-at"

    # Configuration form fields (in addition to the inherited webhook fields)
    recipient_filter = forms.EmailField(
        label=_("Recipient filter"),
        required=False,
        help_text=_("Only trigger for emails to this recipient (optional)"),
    )
    subject_contains = forms.CharField(
        label=_("Subject contains"),
        required=False,
        help_text=_("Only trigger if subject contains this text (optional)"),
    )
    status_filter = forms.ChoiceField(
        label=_("Status filter"),
        choices=[
            ("", _("Any")),
            ("received", _("Received")),
            ("queued", _("Queued")),
            ("sent", _("Sent")),
            ("bounced", _("Bounced")),
            ("opened", _("Opened")),
        ],
        required=False,
        help_text=_("Only trigger for this email status"),
    )

    data_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["message_id", "recipient", "timestamp"],
        "properties": {
            "message_id": {"type": "string", "minLength": 1},
            "recipient": {"type": "string", "format": "email"},
            "sender": {"type": "string"},
            "subject": {"type": "string"},
            "body_text": {"type": "string"},
            "timestamp": {"type": "string", "format": "date-time"},
            "status": {"type": "string", "enum": ["received", "queued", "sent", "bounced", "opened"]},
            "provider": {"type": "string"},
        },
        "additionalProperties": True,
    }

    #: Accepted field aliases (first match wins), covering common providers.
    field_aliases = {
        "message_id": ("message_id", "Message-Id", "MessageID", "message-id"),
        "recipient": ("recipient", "to", "To", "ToFull"),
        "sender": ("sender", "from", "From", "FromFull"),
        "subject": ("subject", "Subject"),
        "body_text": ("body_text", "text", "TextBody", "body-plain", "body"),
        "status": ("status", "event", "RecordType"),
        "timestamp": ("timestamp", "date", "Date"),
    }

    def normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Normalize a provider payload row to the trigger's data schema.

        Original keys are preserved (the schema allows additional
        properties); normalized keys take precedence.
        """
        normalized = dict(row)
        for target, aliases in self.field_aliases.items():
            for alias in aliases:
                value = row.get(alias)
                if value not in (None, ""):
                    normalized[target] = value
                    break
        normalized.setdefault("status", "received")
        if isinstance(normalized.get("status"), str):
            normalized["status"] = normalized["status"].lower()
        if not normalized.get("timestamp"):
            normalized["timestamp"] = now().isoformat()
        return normalized

    def matches_filters(self, row: dict[str, Any], config: dict[str, Any]) -> bool:
        """Apply the trigger's configured mail filters to a normalized row."""
        config = config or {}
        recipient_filter = config.get("recipient_filter")
        if recipient_filter and str(row.get("recipient", "")).lower() != recipient_filter.lower():
            return False
        subject_contains = config.get("subject_contains")
        if subject_contains and subject_contains.lower() not in str(row.get("subject", "")).lower():
            return False
        status_filter = config.get("status_filter")
        if status_filter and row.get("status") != status_filter:
            return False
        return True

    def parse_payload(self, request, config: dict[str, Any]) -> list[dict[str, Any]]:
        rows = super().parse_payload(request, config)
        return [
            normalized
            for normalized in (self.normalize_row(row) for row in rows)
            if self.matches_filters(normalized, config)
        ]


# Timer Trigger schema: expects scheduled time and optional recurrence config
class TimerTrigger(Trigger):
    id = "timer"
    name = _("Timer")
    description = _("Starts at a scheduled time or recurring interval (e.g., daily, weekly).")
    icon = "bi-alarm"

    # Configuration form fields
    scheduled_at = forms.DateTimeField(
        label=_("Scheduled at"),
        help_text=_("When should this trigger fire?"),
        required=True,
        widget=widgets.AdminSplitDateTime(),
    )
    recurrence_frequency = forms.ChoiceField(
        label=_("Recurrence frequency"),
        choices=[
            ("", "---"),
            ("hourly", _("Hourly")),
            ("daily", _("Daily")),
            ("weekly", _("Weekly")),
            ("monthly", _("Monthly")),
        ],
        required=False,
        help_text=_("How often should this trigger repeat?"),
    )
    recurrence_interval = forms.IntegerField(
        label=_("Recurrence interval"),
        initial=1,
        min_value=1,
        required=False,
        help_text=_("Repeat every N frequency units (e.g., every 2 days)"),
    )
    recurrence_end_date = forms.DateTimeField(
        label=_("Recurrence end date"),
        required=False,
        help_text=_("When should the recurrence stop? (optional)"),
    )
    recurrence_count = forms.IntegerField(
        label=_("Recurrence count"),
        min_value=1,
        required=False,
        help_text=_("Maximum number of occurrences (alternative to end date)"),
    )

    data_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["scheduled_at"],
        "properties": {
            "scheduled_at": {
                "type": "string",
                "format": "date-time",
                "description": "ISO 8601 timestamp when the trigger should fire",
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone identifier (e.g., 'Europe/Berlin')",
                "default": "UTC",
            },
            "recurrence": {
                "type": "object",
                "description": "Optional recurring schedule configuration",
                "properties": {
                    "frequency": {
                        "type": "string",
                        "enum": ["hourly", "daily", "weekly", "monthly"],
                        "description": "How often the trigger repeats",
                    },
                    "interval": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Repeat every N frequency units (e.g., every 2 days)",
                        "default": 1,
                    },
                    "end_date": {
                        "type": "string",
                        "format": "date-time",
                        "description": "When to stop recurring (optional)",
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of occurrences (alternative to end_date)",
                    },
                },
                "required": ["frequency"],
            },
            "metadata": {
                "type": "object",
                "description": "Additional context or configuration data",
            },
        },
        "additionalProperties": True,
    }


class CodeTrigger(Trigger):
    id = "code"
    name = _("Automation")
    description = _("Starts when triggered by another automation.")
    icon = "bi-code-slash"
    data_schema = {}


class FormSubmissionTrigger(Trigger):
    id = "form_submission"
    name = _("Form Submission")
    description = _("Starts when a form is submitted.")
    icon = "bi-ui-checks"
    data_schema = {}


if apps.is_installed("djangocms_form_builder"):
    from djangocms_form_builder.actions import FormAction, register

    @register
    class AutomationAction(FormAction):
        verbose_name = _("Trigger automation")

        class Meta:
            entangled_fields = {
                "action_parameters": [
                    "trigger",
                ]
            }

        trigger = forms.ModelChoiceField(
            label=_("Automation to trigger"),
            queryset=None,  # Set in __init__
            required=True,
            help_text=_("Select an automation to start upon form submission."),
        )

        def __init__(self, *args, **kwargs):
            from djangocms_automation.models import Automation

            super().__init__(*args, **kwargs)
            qs = Automation.objects.filter(
                is_active=True,
                contents__isnull=False,
                contents__triggers__type="form_submission",
            ).distinct()
            if args:
                self.fields["trigger"].queryset = qs

        def execute(self, form, request):
            from .models import AutomationTrigger

            automation = self.get_parameter(form, "trigger")
            qs = AutomationTrigger.objects.filter(
                automation_content__automation_id=automation["pk"],
                type="form_submission",
            )
            for trigger in qs:
                trigger.trigger_execution(
                    data=[
                        {
                            "data": cleaned_data_to_json_serializable(form.cleaned_data),
                            "user_id": request.user.pk if request.user.is_authenticated else None,
                        }
                    ],
                    start=True,
                )


# Register example triggers
trigger_registry.register(ClickTrigger)
trigger_registry.register(MailTrigger)
trigger_registry.register(TimerTrigger)
trigger_registry.register(CodeTrigger)
trigger_registry.register(FormSubmissionTrigger)
trigger_registry.register(GenericWebhookTrigger)

__all__ = [
    "Trigger",
    "TriggerRegistry",
    "trigger_registry",
    "generate_webhook_token",
    "WebhookTrigger",
    "GenericWebhookTrigger",
    "ClickTrigger",
    "MailTrigger",
    "TimerTrigger",
]

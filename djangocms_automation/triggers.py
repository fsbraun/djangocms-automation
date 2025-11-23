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

from typing import Any, Dict, List, Callable, Optional

from django import forms
from django.utils.translation import gettext_lazy as _

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
    """

    id: str
    name: str
    description: str
    icon: str
    data_schema: Dict[str, Any] = {}

    def validate_payload(
        self,
        payload: Dict[str, Any],
        validator: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
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
        self._triggers: Dict[str, Trigger] = {}

    def register(self, trigger: Trigger):
        self._triggers[trigger.id] = trigger

    def unregister(self, trigger_id: str):
        self._triggers.pop(trigger_id, None)

    def get(self, trigger_id: str) -> Trigger | None:
        return self._triggers.get(trigger_id)

    def all(self) -> List[Trigger]:
        return list(self._triggers.values())

    def get_choices(self) -> List[tuple[str, str]]:
        return [(t.id, t.name) for t in self._triggers.values()]


# Global registry instance
trigger_registry = TriggerRegistry()

# ---------------------------------------------------------------------------
# Example concrete trigger definitions
# ---------------------------------------------------------------------------

# Click Trigger schema: expects element metadata and optional context
class ClickTrigger(Trigger):
    id = "click"
    name = _("Click")
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


# Mail Trigger schema: expects recipient, subject and status data
class MailTrigger(Trigger):
    id = "mail"
    name = _("Mail")
    description = _("Starts when an email is sent or its status updates.")
    icon = "bi-envelope-at"
    data_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["message_id", "recipient", "timestamp"],
        "properties": {
            "message_id": {"type": "string", "minLength": 1},
            "recipient": {"type": "string", "format": "email"},
            "subject": {"type": "string"},
            "timestamp": {"type": "string", "format": "date-time"},
            "status": {"type": "string", "enum": ["queued", "sent", "bounced", "opened"]},
            "provider": {"type": "string"},
        },
        "additionalProperties": True,
    }


# Timer Trigger schema: expects scheduled time and optional recurrence config
class TimerTrigger(Trigger):
    id = "timer"
    name = _("Timer")
    description = _("Starts at a scheduled time or recurring interval (e.g., daily, weekly).")
    icon = "bi-alarm"
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


# Register example triggers
trigger_registry.register(ClickTrigger)
trigger_registry.register(MailTrigger)
trigger_registry.register(TimerTrigger)
trigger_registry.register(CodeTrigger)

__all__ = [
    "Trigger",
    "TriggerRegistry",
    "trigger_registry",
    "ClickTrigger",
    "MailTrigger",
    "TimerTrigger",
]

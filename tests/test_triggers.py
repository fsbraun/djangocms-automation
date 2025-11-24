"""Tests for trigger registry and schema validation."""

import pytest
from jsonschema import ValidationError

from djangocms_automation.triggers import (
    trigger_registry,
    ClickTrigger,
    MailTrigger,
    TimerTrigger,
)


class TestTriggerRegistry:
    def test_registry_contains_examples(self):
        ids = {t.id for t in trigger_registry.all()}
        assert "click" in ids
        assert "mail" in ids
        assert "timer" in ids

    def test_get_choices(self):
        choices = trigger_registry.get_choices()
        assert ("click", "Click") in choices
        assert ("mail", "Mail") in choices
        assert ("timer", "Timer") in choices

    def test_get(self):
        assert trigger_registry.get("click") is ClickTrigger
        assert trigger_registry.get("mail") is MailTrigger
        assert trigger_registry.get("timer") is TimerTrigger
        assert trigger_registry.get("missing") is None


class TestTriggerValidation:
    def test_click_trigger_valid_payload(self):
        payload = {
            "element_id": "btn-login",
            "timestamp": "2025-11-22T10:00:00Z",
            "path": "/login/",
            "metadata": {"role": "primary"},
        }
        trigger = ClickTrigger()
        assert trigger.validate_payload(payload) is True

    def test_click_trigger_missing_required(self):
        payload = {
            "element_id": "btn-login",
            # missing timestamp
        }
        trigger = ClickTrigger()
        with pytest.raises(ValidationError):
            trigger.validate_payload(payload)

    def test_click_trigger_missing_required_no_raise(self):
        payload = {"element_id": "btn-login"}
        trigger = ClickTrigger()
        assert trigger.validate_payload(payload, raise_errors=False) is False

    def test_mail_trigger_valid_payload(self):
        payload = {
            "message_id": "abc123",
            "recipient": "user@example.com",
            "timestamp": "2025-11-22T10:01:00Z",
            "subject": "Welcome",
            "status": "sent",
        }
        trigger = MailTrigger()
        assert trigger.validate_payload(payload) is True

    def test_mail_trigger_invalid_enum(self):
        payload = {
            "message_id": "abc123",
            "recipient": "user@example.com",
            "timestamp": "2025-11-22T10:01:00Z",
            "subject": "Welcome",
            "status": "delivered",  # not in enum
        }
        trigger = MailTrigger()
        with pytest.raises(ValidationError):
            trigger.validate_payload(payload)

    def test_custom_validator_pass_through(self):
        calls = []

        def custom(schema, payload):  # should be invoked instead of builtin
            calls.append("called")
            # emulate success
        payload = {"element_id": "x", "timestamp": "2025-11-22T10:00:00Z"}
        trigger = ClickTrigger()
        assert trigger.validate_payload(payload, validator=custom) is True
        assert calls == ["called"]

    def test_custom_validator_failure(self):
        def custom(schema, payload):
            raise ValidationError("boom")
        trigger = ClickTrigger()
        with pytest.raises(ValidationError):
            trigger.validate_payload({"element_id": "x"}, validator=custom)

    def test_missing_required_fields_fallback_when_jsonschema_absent(monkeypatch):
        """Simulate absence of jsonschema to exercise fallback logic.

        We monkeypatch the Draft202012Validator reference to None; the method should
        then perform shallow required checks and raise ValueError for missing fields.
        """
        from djangocms_automation import triggers as triggers_mod

        original_validator = triggers_mod.Draft202012Validator
        try:
            triggers_mod.Draft202012Validator = None  # force fallback
            trigger = ClickTrigger()
            with pytest.raises(ValueError):
                trigger.validate_payload({"element_id": "only"})
        finally:
            triggers_mod.Draft202012Validator = original_validator

    def test_timer_trigger_valid_payload_simple(self):
        payload = {
            "scheduled_at": "2025-12-01T09:00:00Z",
        }
        trigger = TimerTrigger()
        assert trigger.validate_payload(payload) is True

    def test_timer_trigger_valid_payload_with_timezone(self):
        payload = {
            "scheduled_at": "2025-12-01T09:00:00+01:00",
            "timezone": "Europe/Berlin",
        }
        trigger = TimerTrigger()
        assert trigger.validate_payload(payload) is True

    def test_timer_trigger_valid_payload_with_recurrence(self):
        payload = {
            "scheduled_at": "2025-12-01T09:00:00Z",
            "recurrence": {
                "frequency": "daily",
                "interval": 2,
                "end_date": "2025-12-31T23:59:59Z",
            },
        }
        trigger = TimerTrigger()
        assert trigger.validate_payload(payload) is True

    def test_timer_trigger_valid_payload_with_count(self):
        payload = {
            "scheduled_at": "2025-12-01T09:00:00Z",
            "recurrence": {
                "frequency": "weekly",
                "count": 10,
            },
        }
        trigger = TimerTrigger()
        assert trigger.validate_payload(payload) is True

    def test_timer_trigger_missing_scheduled_at(self):
        payload = {
            "timezone": "Europe/Berlin",
        }
        trigger = TimerTrigger()
        with pytest.raises(ValidationError):
            trigger.validate_payload(payload)

    def test_timer_trigger_invalid_frequency(self):
        payload = {
            "scheduled_at": "2025-12-01T09:00:00Z",
            "recurrence": {
                "frequency": "yearly",  # not in enum
            },
        }
        trigger = TimerTrigger()
        with pytest.raises(ValidationError):
            trigger.validate_payload(payload)

    def test_timer_trigger_invalid_interval(self):
        payload = {
            "scheduled_at": "2025-12-01T09:00:00Z",
            "recurrence": {
                "frequency": "daily",
                "interval": 0,  # minimum is 1
            },
        }
        trigger = TimerTrigger()
        with pytest.raises(ValidationError):
            trigger.validate_payload(payload)


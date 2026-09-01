"""Tests for trigger registry and schema validation."""

import pytest
from jsonschema import ValidationError

from djangocms_automation.triggers import (
    ClickTrigger,
    MailTrigger,
    TimerTrigger,
    trigger_registry,
)


class TestTriggerRegistry:
    def test_registry_contains_examples(self):
        ids = {t.id for t in trigger_registry.all()}
        assert "click" in ids
        assert "mail" in ids
        assert "timer" in ids

    def test_get_choices(self):
        choices = trigger_registry.get_choices()
        assert ("click", "Manual") in choices
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

    def test_trigger_missing_required(self):
        payload = {
            "message_id": "abc123",
            "recipient": "user@example.com",
            # missing timestamp
        }
        trigger = MailTrigger()
        with pytest.raises(ValidationError):
            trigger.validate_payload(payload)

    def test_trigger_missing_required_no_raise(self):
        payload = {"message_id": "abc123"}
        trigger = MailTrigger()
        assert trigger.validate_payload(payload, raise_errors=False) is False

    def test_the_manual_trigger_requires_nothing(self):
        """What starts one is a person choosing it, not a click on an element.

        Demanding an element id and a timestamp from them would be asking for a
        description of an event that did not happen.
        """
        assert ClickTrigger().validate_payload({}) is True
        # And a caller already sending the old fields is still accepted.
        assert ClickTrigger().validate_payload({"element_id": "btn", "timestamp": "2026-01-01T00:00:00Z"}) is True

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
            trigger = MailTrigger()
            with pytest.raises(ValueError):
                trigger.validate_payload({"message_id": "only"})
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


@pytest.mark.django_db
class TestRenamingATrigger:
    """A trigger's slot is how it finds the placeholder holding its flow."""

    @pytest.fixture
    def trigger(self, admin_user):
        from cms.api import add_plugin
        from cms.models import Placeholder
        from django.contrib.contenttypes.models import ContentType

        from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger

        automation = Automation.objects.create(name="Renaming", is_active=True)
        content = AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Renaming")
        trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")
        placeholder = Placeholder.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(AutomationContent),
            object_id=content.pk,
            slot="start",
        )[0]
        add_plugin(placeholder=placeholder, plugin_type="UserInputAction", language="en")
        return trigger

    def test_the_placeholder_follows_the_name(self, trigger):
        """Renaming one without the other leaves the plugins where nothing
        looks for them — and the editor still shows the workflow, so the only
        symptom is every run failing with a message about a missing
        placeholder, which does not sound like "you renamed something"."""
        trigger.slot = "kickoff"
        trigger.save()

        assert list(trigger.placeholders().values_list("slot", flat=True)) == ["kickoff"]

    def test_the_flow_still_runs_after_a_rename(self, trigger, settings):
        settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}

        trigger.slot = "kickoff"
        trigger.save()

        instance = trigger.trigger_execution(data=[{"seed": 1}], start=False)
        assert instance is not None, "the plugins are still found"

    def test_saving_without_renaming_leaves_the_placeholder_alone(self, trigger):
        trigger.position = 3
        trigger.save()

        assert list(trigger.placeholders().values_list("slot", flat=True)) == ["start"]

    def test_a_new_trigger_renames_nothing(self, trigger):
        """A create has no previous name, and another trigger's placeholder is
        not this one's to touch."""
        from djangocms_automation.models import AutomationTrigger

        AutomationTrigger.objects.create(automation_content=trigger.automation_content, slot="second", type="code")

        assert sorted(trigger.placeholders().values_list("slot", flat=True)) == ["start"]


@pytest.mark.django_db
class TestOneSlotPerTrigger:
    """The slot names the placeholder holding a trigger's flow."""

    @pytest.fixture
    def content(self, admin_user):
        from djangocms_automation.models import Automation, AutomationContent

        automation = Automation.objects.create(name="Slots", is_active=True)
        return AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Slots")

    def test_the_form_says_so_before_the_database_does(self, content):
        """A ModelForm validates constraints as well as uniqueness, so the
        message reaches the editor instead of an IntegrityError reaching the
        logs."""
        from djangocms_automation.forms import AutomationTriggerAdminForm
        from djangocms_automation.models import AutomationTrigger

        AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")

        form = AutomationTriggerAdminForm(
            data={"automation_content": content.pk, "slot": "start", "type": "click", "position": 1}
        )

        assert not form.is_valid()
        assert "already uses this slot" in str(form.errors)

    def test_the_database_refuses_it_too(self, content):
        """Nothing reaches the model only through a form."""
        from django.db import IntegrityError, transaction

        from djangocms_automation.models import AutomationTrigger

        AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")

        with pytest.raises(IntegrityError), transaction.atomic():
            AutomationTrigger.objects.create(automation_content=content, slot="start", type="code")

    def test_the_same_slot_on_another_automation_is_fine(self, content, admin_user):
        """Slots are named within an automation, and *start* is the obvious
        name for the first one everywhere."""
        from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger

        other = AutomationContent.objects.with_user(admin_user).create(
            automation=Automation.objects.create(name="Elsewhere", is_active=True), description="Elsewhere"
        )
        AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")

        AutomationTrigger.objects.create(automation_content=other, slot="start", type="click")

    def test_renaming_onto_a_taken_slot_is_refused(self, content):
        """Which is the case this exists for: the rename would carry the
        placeholder with it and leave two placeholders sharing one name."""
        from django.db import IntegrityError, transaction

        from djangocms_automation.models import AutomationTrigger

        first = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")
        second = AutomationTrigger.objects.create(automation_content=content, slot="second", type="code")

        second.slot = first.slot
        with pytest.raises(IntegrityError), transaction.atomic():
            second.save()

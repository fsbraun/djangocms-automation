"""Tests for trigger registry and schema validation."""

import pytest
from django.urls import reverse
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


@pytest.mark.django_db
class TestDeletingATrigger:
    """A trigger's flow lives in a placeholder found by slot."""

    @pytest.fixture
    def trigger(self, admin_user):
        from cms.api import add_plugin
        from cms.models import Placeholder
        from django.contrib.contenttypes.models import ContentType

        from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger

        automation = Automation.objects.create(name="Deleting", is_active=True)
        content = AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Deleting")
        trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")
        placeholder = Placeholder.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(AutomationContent),
            object_id=content.pk,
            slot="start",
        )[0]
        add_plugin(placeholder=placeholder, plugin_type="UserInputAction", language="en")
        return trigger

    def test_the_flow_goes_with_it(self, trigger):
        """An orphaned placeholder is invisible in the editor, still holds
        every plugin, and is quietly reclaimed by the next trigger that takes
        the same name — somebody else's flow under a new entry point."""
        from cms.models import CMSPlugin

        placeholder = trigger.placeholder()
        assert placeholder is not None

        trigger.delete()

        assert trigger.placeholders().filter(slot="start").first() is None
        assert not CMSPlugin.objects.filter(placeholder_id=placeholder.pk).exists()

    def test_a_bulk_delete_takes_it_too(self, trigger):
        """``queryset.delete()`` never calls ``Model.delete``, and that is how
        the admin's own bulk action removes things."""
        from djangocms_automation.models import AutomationTrigger

        placeholders = trigger.placeholders()

        AutomationTrigger.objects.filter(pk=trigger.pk).delete()

        assert placeholders.filter(slot="start").first() is None

    def test_another_triggers_flow_is_left_alone(self, trigger):
        """Only the slot that was this trigger's."""
        from cms.models import Placeholder
        from django.contrib.contenttypes.models import ContentType

        from djangocms_automation.models import AutomationContent, AutomationTrigger

        other = AutomationTrigger.objects.create(
            automation_content=trigger.automation_content, slot="second", type="code"
        )
        Placeholder.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(AutomationContent),
            object_id=trigger.automation_content.pk,
            slot="second",
        )

        trigger.delete()

        assert other.placeholders().filter(slot="second").exists()

    def test_the_confirmation_says_the_flow_is_going(self, admin_client, trigger):
        """Django lists what a delete cascades to by following foreign keys,
        and a placeholder found by slot is not one. Deleting a workflow should
        not be something you discover afterwards."""
        from django.urls import reverse

        body = admin_client.get(
            reverse("admin:djangocms_automation_automationtrigger_delete", args=[trigger.pk])
        ).content.decode()

        assert "Flow held by this trigger" in body
        assert "1 step(s)" in body


@pytest.mark.django_db
class TestATriggerIsAsEditableAsItsFlow:
    """A trigger names a flow, so it is exactly as editable as the flow."""

    @pytest.fixture
    def content(self, admin_user):
        from djangocms_automation.models import Automation, AutomationContent

        automation = Automation.objects.create(name="Gated", is_active=True)
        return AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Gated")

    def test_a_draft_may_be_changed(self, content, admin_user, rf):
        from djangocms_automation.models import AutomationTrigger

        trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")
        request = rf.get("/")
        request.user = admin_user

        assert content.placeholder_editable(request) is True
        assert trigger.placeholder_editable(request) is True

    def test_a_locked_version_may_not(self, content, admin_user, rf, monkeypatch):
        """Versioning answers this by adding a check to the placeholder field.

        Patched here rather than published for real: what matters is that the
        answer is *taken from* that check rather than decided locally.
        """
        from djangocms_automation.models import AutomationTrigger

        trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")
        field = content._meta.get_field("placeholders")
        # ``checks`` chains the class-level defaults with the field's own, and
        # is read-only; the field's own list is the one to add to.
        monkeypatch.setattr(field, "_checks", [lambda placeholder, user: False])

        request = rf.get("/")
        request.user = admin_user

        assert content.placeholder_editable(request) is False
        assert trigger.placeholder_editable(request) is False

    def test_asking_does_not_go_looking_for_a_placeholder(self, content, admin_user, rf):
        """The checks read nothing off a placeholder but ``source``, so the one
        they are asked through is never saved and never fetched.

        Asserted as "no placeholder query" rather than "no queries at all":
        resolving the content type costs one the first time in a process, and
        that is cached rather than avoidable."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        request = rf.get("/")
        request.user = admin_user

        with CaptureQueriesContext(connection) as queries:
            content.placeholder_editable(request)

        touched = [q["sql"] for q in queries if "cms_placeholder" in q["sql"]]
        assert touched == [], f"a placeholder was fetched after all: {touched}"

    def test_the_admin_refuses_what_the_flow_refuses(self, content, admin_user, rf, monkeypatch):
        from django.contrib import admin as django_admin

        from djangocms_automation.admin import AutomationTriggerAdmin
        from djangocms_automation.models import AutomationTrigger

        trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")
        site = AutomationTriggerAdmin(AutomationTrigger, django_admin.site)
        request = rf.get("/")
        request.user = admin_user

        assert site.has_change_permission(request, trigger) is True

        field = content._meta.get_field("placeholders")
        monkeypatch.setattr(field, "_checks", [lambda placeholder, user: False])

        assert site.has_change_permission(request, trigger) is False
        assert site.has_delete_permission(request, trigger) is False

    def test_adding_is_refused_too(self, content, admin_user, rf, monkeypatch):
        """The add view has no object, so the automation comes from the query
        parameter the editor's *Add Trigger* link carries."""
        from django.contrib import admin as django_admin

        from djangocms_automation.admin import AutomationTriggerAdmin
        from djangocms_automation.models import AutomationTrigger

        site = AutomationTriggerAdmin(AutomationTrigger, django_admin.site)
        request = rf.get("/", {"automation_content": content.pk})
        request.user = admin_user

        assert site.has_add_permission(request) is True

        field = content._meta.get_field("placeholders")
        monkeypatch.setattr(field, "_checks", [lambda placeholder, user: False])

        assert site.has_add_permission(request) is False


def test_the_checks_are_reached_through_meta_not_the_attribute():
    """``self.placeholders`` is the descriptor's related manager, not the field.

    Worth pinning: the attribute reads like the field and answers to nothing
    the field answers to, so reaching for it looks right and fails.
    """
    from cms.models.fields import PlaceholderRelationField

    from djangocms_automation.models import AutomationContent

    field = AutomationContent._meta.get_field("placeholders")
    assert isinstance(field, PlaceholderRelationField)
    assert hasattr(field, "run_checks")
    assert not hasattr(AutomationContent.placeholders, "run_checks"), "the descriptor is not the field"


@pytest.mark.django_db
class TestCopyingAVersion:
    """A new version needs its own flows *and* its own way in."""

    @pytest.fixture
    def content(self, admin_user):
        from cms.api import add_plugin
        from cms.models import Placeholder
        from django.contrib.contenttypes.models import ContentType

        from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger

        automation = Automation.objects.create(name="Copying", is_active=True)
        content = AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Copying")
        AutomationTrigger.objects.create(
            automation_content=content, slot="start", type="click", config={"note": "keep me"}, position=0
        )
        AutomationTrigger.objects.create(automation_content=content, slot="second", type="code", position=1)
        placeholder = Placeholder.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(AutomationContent),
            object_id=content.pk,
            slot="start",
        )[0]
        add_plugin(placeholder=placeholder, plugin_type="UserInputAction", language="en")
        return content

    def test_the_triggers_come_too(self, content):
        """``default_copy`` follows placeholder relations and CMS extensions.

        Triggers are a reverse foreign key and it cannot know about them, so a
        new draft would carry every plugin and no way in — the workflow visible
        in the editor and nothing able to start it.
        """
        copy = content.copy()

        assert copy.pk != content.pk
        assert sorted(copy.triggers.values_list("slot", "type")) == [("second", "code"), ("start", "click")]
        assert copy.triggers.get(slot="start").config == {"note": "keep me"}

    def test_the_copied_trigger_finds_the_copied_flow(self, content):
        """A trigger finds its flow by slot, so copying the placeholders under
        the same names is what makes this work at all."""
        from cms.models import CMSPlugin

        copy = content.copy()

        placeholder = copy.triggers.get(slot="start").placeholder()
        assert placeholder is not None
        assert placeholder.pk != content.triggers.get(slot="start").placeholder().pk, "its own, not shared"
        assert CMSPlugin.objects.filter(placeholder=placeholder).count() == 1

    def test_the_original_keeps_its_own(self, content):
        content.copy()

        assert content.triggers.count() == 2
        assert content.triggers.get(slot="start").placeholder() is not None


def test_copying_needs_nothing_from_versioning():
    """The copy is ours, so only the registration knows versioning exists.

    Written out rather than wrapping ``default_copy``: everything it does is
    django CMS or Django, and an import here would put a hard dependency in the
    middle of the model layer.
    """
    import inspect

    from djangocms_automation import models

    source = inspect.getsource(models)
    imports = [
        line
        for line in source.splitlines()
        if "djangocms_versioning" in line and line.strip().startswith(("import", "from"))
    ]
    assert imports == [], f"models.py should not import versioning: {imports}"


def test_the_editability_check_stays_out_of_versionings_way():
    """``is_editable`` is not a free name on a versioned content model.

    djangocms-versioning patches its own onto the class —
    ``is_editable(content_obj, request)`` — so a method of ours with that name
    and a ``user`` argument would replace it and be called with the wrong
    thing. Ours says what it asks about instead.
    """
    from djangocms_automation.models import AutomationContent, AutomationTrigger

    for model in (AutomationContent, AutomationTrigger):
        assert "placeholder_editable" in model.__dict__, f"{model.__name__} lost the method"

    injected = AutomationContent.__dict__.get("is_editable")
    if injected is not None:
        assert injected.__module__ != "djangocms_automation.models", "that name is versioning's, not ours"


@pytest.mark.django_db
def test_editability_is_asked_with_a_request(admin_user, rf):
    """The signature django CMS uses everywhere else for the same question.

    The checks underneath want a user, but a caller holding a request should
    not have to remember which of the two this one asks for.
    """
    import inspect

    from djangocms_automation.models import AutomationContent, AutomationTrigger

    for model in (AutomationContent, AutomationTrigger):
        parameters = list(inspect.signature(model.placeholder_editable).parameters)
        assert parameters == ["self", "request"], f"{model.__name__}: {parameters}"


def _admin_form(trigger):
    """The form as the admin builds it: config fields come from the trigger type."""
    from django.contrib import admin as django_admin
    from django.contrib.auth import get_user_model
    from django.test import RequestFactory

    from djangocms_automation.admin import AutomationTriggerAdmin
    from djangocms_automation.models import AutomationTrigger

    site = AutomationTriggerAdmin(AutomationTrigger, django_admin.site)
    request = RequestFactory().get("/")
    # ``get_form`` asks the admin about permissions, which asks the user.
    request.user = get_user_model().objects.filter(is_superuser=True).first()
    return site.get_form(request, trigger)(instance=trigger)


@pytest.mark.django_db
class TestTimerTriggerRoundTrip:
    """JSON has no datetime, so a timer's config stores a string."""

    @pytest.fixture
    def trigger(self, admin_user):
        from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger

        automation = Automation.objects.create(name="Timing", is_active=True)
        content = AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Timing")
        return AutomationTrigger.objects.create(
            automation_content=content,
            slot="start",
            type="timer",
            config={
                "scheduled_at": "2026-08-28T16:28:15.764361+00:00",
                "recurrence_frequency": "daily",
                "recurrence_interval": 1,
            },
        )

    def test_the_change_form_opens(self, admin_client, trigger):
        """It did not. A widget that splits a value across two inputs calls
        ``decompress`` on the initial, and a string has no ``utcoffset`` — so
        the page raised ``AttributeError`` before rendering."""
        response = admin_client.get(
            reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk]) + "?_popup=1"
        )

        assert response.status_code == 200
        assert "2026-08-28" in response.content.decode(), "and shows what was stored"

    def test_the_stored_string_becomes_a_datetime(self, trigger, admin_user):
        import datetime

        form = _admin_form(trigger)

        assert isinstance(form.initial["scheduled_at"], datetime.datetime)
        assert form.initial["recurrence_interval"] == 1, "and anything else is left as it is"

    def test_something_unparseable_is_handed_back_as_it_stands(self, trigger, admin_user):
        """A form field reporting a bad value is more use than a config key
        silently emptied."""
        trigger.config = {**trigger.config, "scheduled_at": "not a date"}
        trigger.save()

        form = _admin_form(trigger)

        assert form.initial["scheduled_at"] == "not a date"

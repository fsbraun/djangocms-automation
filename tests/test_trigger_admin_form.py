"""The trigger admin must not leak one request's trigger type into another.

``AutomationTriggerAdmin`` composes a form class from the base admin form and
the selected trigger's config fields. That class used to be assigned to
``self.form``; because Django instantiates a ``ModelAdmin`` once and shares it
across every request, the assignment let concurrent requests overwrite each
other's form.
"""

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from django.urls import reverse

from djangocms_automation.admin import AutomationTriggerAdmin
from djangocms_automation.forms import AutomationTriggerAdminForm
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger

TIMER_FIELDS = {"scheduled_at", "recurrence_frequency"}
WEBHOOK_FIELDS = {"token", "signing_secret"}


@pytest.fixture
def trigger_admin():
    return AutomationTriggerAdmin(AutomationTrigger, AdminSite())


@pytest.fixture
def content(db, admin_user):
    automation = Automation.objects.create(name="Trigger admin", is_active=True)
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation, description="Trigger admin content"
    )


def add_request(admin_user, trigger_type):
    request = RequestFactory().get(f"/add/?type={trigger_type}")
    request.user = admin_user
    return request


@pytest.mark.django_db
def test_get_form_does_not_mutate_the_shared_admin(trigger_admin, admin_user):
    """The ModelAdmin singleton must be left exactly as it was."""
    before = trigger_admin.form
    trigger_admin.get_form(add_request(admin_user, "webhook"))
    assert trigger_admin.form is before is AutomationTriggerAdminForm


@pytest.mark.django_db
def test_each_trigger_type_gets_its_own_config_fields(trigger_admin, admin_user):
    timer_form = trigger_admin.get_form(add_request(admin_user, "timer"))
    webhook_form = trigger_admin.get_form(add_request(admin_user, "webhook"))

    assert TIMER_FIELDS <= set(timer_form.base_fields)
    assert WEBHOOK_FIELDS <= set(webhook_form.base_fields)


@pytest.mark.django_db
def test_a_built_form_is_not_altered_by_later_calls(trigger_admin, admin_user):
    """A form class already handed out stays correct as other requests arrive."""
    timer_form = trigger_admin.get_form(add_request(admin_user, "timer"))
    trigger_admin.get_form(add_request(admin_user, "webhook"))
    timer_form_again = trigger_admin.get_form(add_request(admin_user, "timer"))

    assert not (WEBHOOK_FIELDS & set(timer_form.base_fields))
    assert not (WEBHOOK_FIELDS & set(timer_form_again.base_fields))
    assert TIMER_FIELDS <= set(timer_form_again.base_fields)


@pytest.mark.django_db
def test_interleaved_requests_keep_their_own_fields(trigger_admin, admin_user):
    """Interleaving is what a real server does; each form must stay correct."""
    timer_form = trigger_admin.get_form(add_request(admin_user, "timer"))
    webhook_form = trigger_admin.get_form(add_request(admin_user, "webhook"))
    trigger_admin.get_form(add_request(admin_user, "click"))

    assert TIMER_FIELDS <= set(timer_form.base_fields)
    assert WEBHOOK_FIELDS <= set(webhook_form.base_fields)
    assert not (WEBHOOK_FIELDS & set(timer_form.base_fields))


@pytest.mark.django_db
def test_change_form_uses_the_saved_trigger_type(trigger_admin, admin_user, content):
    """An existing trigger renders its own type's fields, not the last one seen."""
    trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="timer", position=0)
    trigger_admin.get_form(add_request(admin_user, "webhook"))

    request = RequestFactory().get("/change/")
    request.user = admin_user
    form = trigger_admin.get_form(request, obj=trigger)

    assert TIMER_FIELDS <= set(form.base_fields)
    assert not (WEBHOOK_FIELDS & set(form.base_fields))


@pytest.mark.django_db
def test_type_change_post_omits_config_fields(trigger_admin, admin_user):
    """Mid-change the config fieldset is dropped, so stale values cannot be saved."""
    request = RequestFactory().post("/add/", {"_trigger_type_change": "webhook"})
    request.user = admin_user
    form = trigger_admin.get_form(request)

    assert not (WEBHOOK_FIELDS & set(form.base_fields))


# --------------------------------------------------------------------------
# Popup marking for the CMS modal
# --------------------------------------------------------------------------


@pytest.fixture
def trigger(db, content):
    return AutomationTrigger.objects.create(automation_content=content, slot="start", type="webhook", position=0)


@pytest.mark.django_db
def test_direct_admin_visit_keeps_the_normal_chrome(admin_client, trigger):
    """Navigating to the admin URL yourself is not a modal; keep the full page."""
    url = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])
    html = admin_client.get(url).content.decode()

    assert "breadcrumbs" in html
    assert 'name="_popup"' not in html


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["language=en", "cms_path=/en/", "edit_fields=slot"])
def test_frontend_edit_links_render_as_a_popup(admin_client, trigger, query):
    """django CMS opens these in its modal but does not flag them itself."""
    url = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])
    html = admin_client.get(f"{url}?{query}").content.decode()

    assert "breadcrumbs" not in html, "admin chrome must not render inside the modal"
    assert 'name="_popup"' in html, "the popup flag must survive into the POST"


@pytest.mark.django_db
def test_add_from_the_automation_editor_renders_as_a_popup(admin_client, content):
    url = reverse("admin:djangocms_automation_automationtrigger_add")
    html = admin_client.get(f"{url}?automation_content={content.pk}").content.decode()

    assert "breadcrumbs" not in html
    assert 'name="_popup"' in html


@pytest.mark.django_db
def test_toolbar_popup_flag_is_left_alone(admin_client, trigger):
    """When the toolbar already flagged the request, do nothing to it."""
    url = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])
    html = admin_client.get(f"{url}?_popup=1").content.decode()

    assert 'name="_popup"' in html


@pytest.mark.django_db
def test_saving_from_the_modal_returns_the_popup_response(admin_client, trigger, content):
    """The popup response is what tells the CMS modal it may close.

    Without it the modal would follow the redirect and display the changelist
    instead of closing.
    """
    url = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])
    response = admin_client.post(
        f"{url}?language=en",
        {
            "automation_content": content.pk,
            "type": "webhook",
            "slot": "start",
            "position": 0,
            "token": "abc",
            "signing_secret": "shh",
            "_popup": "1",
            "_save": "Save",
        },
    )

    assert response.status_code == 200
    assert "django-admin-popup-response-constants" in response.content.decode()


@pytest.mark.django_db
def test_saving_outside_the_modal_still_redirects(admin_client, trigger, content):
    """A direct admin save keeps ordinary admin behaviour."""
    url = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])
    response = admin_client.post(
        url,
        {
            "automation_content": content.pk,
            "type": "webhook",
            "slot": "start",
            "position": 0,
            "token": "abc",
            "signing_secret": "shh",
            "_save": "Save",
        },
    )

    assert response.status_code == 302
    assert response["Location"].endswith("/automationtrigger/")


# --------------------------------------------------------------------------
# Configuration has to survive a round trip through the form
# --------------------------------------------------------------------------


def composed_form(trigger_type, **kwargs):
    from djangocms_automation.triggers import trigger_registry

    definition = trigger_registry.get(trigger_type)
    return type("FormWithTriggerConfig", (AutomationTriggerAdminForm, definition), {})(**kwargs)


@pytest.mark.django_db
def test_a_triggers_configuration_is_shown_when_editing_it(content):
    """Opening a trigger has to show what it is configured with.

    The config fields are declared on the trigger definition, not on the model,
    so nothing seeds them from the stored value automatically. Left empty, the
    form does not merely look wrong — ``clean`` rebuilds the config from what
    was submitted, so opening a trigger and saving it wipes its settings.
    """
    trigger = AutomationTrigger.objects.create(
        automation_content=content,
        slot="start",
        type="timer",
        position=0,
        config={"recurrence_frequency": "daily", "recurrence_interval": 3},
    )

    form = composed_form("timer", instance=trigger)

    assert form["recurrence_frequency"].value() == "daily"
    assert form["recurrence_interval"].value() == 3


@pytest.mark.django_db
def test_saving_a_trigger_unchanged_keeps_its_configuration(content):
    trigger = AutomationTrigger.objects.create(
        automation_content=content,
        slot="start",
        type="timer",
        position=0,
        config={"recurrence_frequency": "daily", "recurrence_interval": 3},
    )

    form = composed_form(
        "timer",
        instance=trigger,
        data={
            "automation_content": content.pk,
            "type": "timer",
            "slot": "start",
            "position": 0,
            # Two inputs, because the admin renders a split date/time widget.
            "scheduled_at_0": "2026-09-01",
            "scheduled_at_1": "09:00:00",
            "recurrence_frequency": "daily",
            "recurrence_interval": 3,
        },
    )
    assert form.is_valid(), form.errors
    form.save()

    trigger.refresh_from_db()
    assert trigger.config["recurrence_frequency"] == "daily"
    assert trigger.config["recurrence_interval"] == 3
    assert trigger.config["scheduled_at"].startswith("2026-09-01T09:00")


@pytest.mark.django_db
def test_a_timer_trigger_can_be_saved_at_all(content):
    """The admin renders "Scheduled at" as two inputs, a date and a time.

    A split widget hands back a two-item list, so the field reading it has to
    be a split field. Paired with a plain ``DateTimeField`` the form raised
    ``'list' object has no attribute 'strip'`` on submit — before validation,
    so no timer trigger could be created or edited.
    """
    form = composed_form(
        "timer",
        data={
            "automation_content": content.pk,
            "type": "timer",
            "slot": "start",
            "position": 0,
            "scheduled_at_0": "2026-09-01",
            "scheduled_at_1": "09:00:00",
        },
    )

    assert form.is_valid(), form.errors
    trigger = form.save()
    assert trigger.config["scheduled_at"].startswith("2026-09-01T09:00")


# --------------------------------------------------------------------------
# The Automation trigger declares what it accepts
# --------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {"email": {"type": "string"}},
    "required": ["email"],
    "additionalProperties": False,
}


@pytest.mark.django_db
def test_an_automation_trigger_declares_the_data_it_accepts(content):
    """A trigger called by something else has to say what to send it.

    Every other trigger type knows its payload from the outside world it
    listens to. This one is called from inside, so the shape is whatever the
    automation was built to expect — which only its author can say.
    """
    form = composed_form(
        "code",
        data={
            "automation_content": content.pk,
            "type": "code",
            "slot": "start",
            "position": 0,
            "data_schema": SCHEMA,
        },
    )
    assert form.is_valid(), form.errors
    trigger = form.save()

    assert trigger.config["data_schema"] == SCHEMA
    assert trigger.data_schema == SCHEMA


@pytest.mark.django_db
def test_a_declared_schema_is_what_a_payload_is_checked_against(content):
    trigger = AutomationTrigger.objects.create(
        automation_content=content, slot="start", type="code", position=0, config={"data_schema": SCHEMA}
    )

    definition = trigger.get_definition()()
    assert definition.validate_payload({"email": "a@example.com"}, config=trigger.config)
    assert not definition.validate_payload({}, config=trigger.config, raise_errors=False)


@pytest.mark.django_db
def test_an_automation_trigger_without_a_schema_accepts_anything(content):
    """Declaring nothing stays legal — it means "I take whatever I am given"."""
    trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="code", position=0)

    assert trigger.data_schema == {}
    assert trigger.get_definition()().validate_payload({"anything": 1}, config=trigger.config)


@pytest.mark.django_db
def test_something_that_is_not_a_schema_is_refused(content):
    form = composed_form(
        "code",
        data={
            "automation_content": content.pk,
            "type": "code",
            "slot": "start",
            "position": 0,
            "data_schema": {"type": "object", "properties": {"email": {"type": "string"}}},
        },
    )

    assert not form.is_valid()
    assert "additionalProperties" in str(form.errors["data_schema"])


EMAIL_SCHEMA = {
    "type": "object",
    "properties": {"email": {"type": "string", "format": "email"}},
    "required": ["email"],
    "additionalProperties": False,
}


@pytest.mark.django_db
def test_a_declared_email_is_checked_not_merely_noted(content):
    """``format`` is an annotation unless a validator is asked to assert it.

    Left as one, a trigger declaring an email would accept "nonsense" — which
    reads to whoever wrote the schema like a constraint they set.
    """
    trigger = AutomationTrigger.objects.create(
        automation_content=content, slot="start", type="code", position=0, config={"data_schema": EMAIL_SCHEMA}
    )
    definition = trigger.get_definition()()

    assert definition.validate_payload({"email": "ann@example.com"}, config=trigger.config)
    assert not definition.validate_payload({"email": "nonsense"}, config=trigger.config, raise_errors=False)


@pytest.mark.django_db
def test_a_schema_declaring_an_email_is_accepted_by_the_form(content):
    form = composed_form(
        "code",
        data={
            "automation_content": content.pk,
            "type": "code",
            "slot": "start",
            "position": 0,
            "data_schema": EMAIL_SCHEMA,
        },
    )

    assert form.is_valid(), form.errors
    assert form.save().data_schema == EMAIL_SCHEMA

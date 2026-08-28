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

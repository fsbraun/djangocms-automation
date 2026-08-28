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

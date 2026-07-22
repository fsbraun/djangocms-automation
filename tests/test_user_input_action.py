"""Tests for the user-input (human-in-the-loop) action and resume flow."""

import pytest

from django.contrib.contenttypes.models import ContentType

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation import engine
from djangocms_automation.actions.user_input import UserInputActionPluginModel
from djangocms_automation.instances import COMPLETED, WAITING, AutomationAction
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="HIL Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="HIL automation content",
    )


@pytest.fixture
def run_setup(automation_content, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="start",
        type="click",
        position=0,
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    return trigger, placeholder


@pytest.mark.django_db
def test_user_input_action_waits_and_resumes(run_setup, admin_user, settings):
    trigger, placeholder = run_setup
    plugin = add_plugin(placeholder=placeholder, plugin_type="UserInputAction", language=settings.LANGUAGE_CODE)
    model = UserInputActionPluginModel.objects.get(pk=plugin.pk)
    model.config = {"note": "Approve order for {{ name }}", "permissions": ""}
    model.save()
    # Follow-up action after the approval step (base action passes data through).
    add_plugin(
        placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, position="last-child"
    )

    trigger.trigger_execution(data=[{"name": "Alice"}], start=True)

    instance = trigger.automation_content.automationinstance_set.first()
    wait_action = AutomationAction.objects.get(automation_instance=instance, plugin_ptr=model.uuid)
    assert wait_action.state == WAITING
    assert wait_action.requires_interaction is True
    assert wait_action.result == {"note": "Approve order for Alice"}
    assert instance.finished is None

    # Open tasks are visible to a permitted user.
    assert wait_action in AutomationAction.get_open_tasks(admin_user)

    # Resume: the follow-up action executes and the instance completes.
    engine.resume_action(wait_action.pk, admin_user, data={"approved": True})

    wait_action.refresh_from_db()
    assert wait_action.state == COMPLETED
    assert wait_action.finished is not None

    actions = AutomationAction.objects.filter(automation_instance=instance)
    assert actions.count() == 2
    assert all(a.state == COMPLETED for a in actions)
    instance.refresh_from_db()
    assert instance.status == COMPLETED
    # The resumed data (original rows + approval row) flowed into the next action.
    follow_up = actions.exclude(pk=wait_action.pk).get()
    assert follow_up.result == [{"name": "Alice"}, {"approved": True}]


@pytest.mark.django_db
def test_user_input_action_permission_denied(run_setup, admin_user, django_user_model, settings):
    trigger, placeholder = run_setup
    plugin = add_plugin(placeholder=placeholder, plugin_type="UserInputAction", language=settings.LANGUAGE_CODE)
    model = UserInputActionPluginModel.objects.get(pk=plugin.pk)
    model.config = {"note": "", "permissions": "auth.change_user"}
    model.save()

    trigger.trigger_execution(data=[], start=True)

    instance = trigger.automation_content.automationinstance_set.first()
    wait_action = AutomationAction.objects.get(automation_instance=instance)
    assert wait_action.state == WAITING

    plain_user = django_user_model.objects.create_user(username="plain", password="x")
    assert wait_action not in AutomationAction.get_open_tasks(plain_user)
    with pytest.raises(PermissionError):
        engine.resume_action(wait_action.pk, plain_user)

    # Superusers may always resume.
    engine.resume_action(wait_action.pk, admin_user)
    wait_action.refresh_from_db()
    assert wait_action.state == COMPLETED

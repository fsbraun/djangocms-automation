"""Tests for the admin open-tasks list / resume views and instance admin displays."""

import uuid

import pytest

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation.actions.user_input import UserInputActionPluginModel
from djangocms_automation.admin import AutomationInstanceAdmin
from djangocms_automation.instances import COMPLETED, FAILED, WAITING, AutomationAction, AutomationInstance
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Admin Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Admin automation content",
    )


@pytest.fixture
def waiting_action(automation_content, settings, admin_user):
    """A real UserInputAction run that is WAITING for interaction."""
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="click", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    plugin = add_plugin(placeholder=placeholder, plugin_type="UserInputAction", language=settings.LANGUAGE_CODE)
    model = UserInputActionPluginModel.objects.get(pk=plugin.pk)
    model.config = {"note": "Please approve {{ subject }}", "permissions": ""}
    model.save()
    trigger.trigger_execution(data=[{"subject": "order 42"}], start=True)
    instance = automation_content.automationinstance_set.first()
    return AutomationAction.objects.get(automation_instance=instance)


@pytest.mark.django_db
def test_open_tasks_view_lists_waiting_tasks(admin_client, waiting_action):
    url = reverse("admin:djangocms_automation_open_tasks")
    response = admin_client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Please approve order 42" in content
    assert reverse("admin:djangocms_automation_resume_action", args=[waiting_action.pk]) in content


@pytest.mark.django_db
def test_open_tasks_view_empty(admin_client, db):
    response = admin_client.get(reverse("admin:djangocms_automation_open_tasks"))
    assert response.status_code == 200
    assert "No open tasks" in response.content.decode()


@pytest.mark.django_db
def test_resume_view_completes_task(admin_client, waiting_action):
    url = reverse("admin:djangocms_automation_resume_action", args=[waiting_action.pk])
    response = admin_client.post(url, follow=True)
    assert response.status_code == 200
    assert "Task resumed." in response.content.decode()
    waiting_action.refresh_from_db()
    assert waiting_action.state == COMPLETED
    waiting_action.automation_instance.refresh_from_db()
    assert waiting_action.automation_instance.status == COMPLETED


@pytest.mark.django_db
def test_resume_view_get_redirects_without_resuming(admin_client, waiting_action):
    url = reverse("admin:djangocms_automation_resume_action", args=[waiting_action.pk])
    response = admin_client.get(url)
    assert response.status_code == 302
    waiting_action.refresh_from_db()
    assert waiting_action.state == WAITING


@pytest.mark.django_db
def test_resume_view_error_for_non_waiting_action(admin_client, automation_content):
    instance = AutomationInstance.objects.create(automation_content=automation_content)
    action = AutomationAction.objects.create(
        automation_instance=instance, plugin_ptr=uuid.uuid4()
    )  # not requiring interaction
    url = reverse("admin:djangocms_automation_resume_action", args=[action.pk])
    response = admin_client.post(url, follow=True)
    assert response.status_code == 200
    assert "not awaiting user interaction" in response.content.decode()


@pytest.mark.django_db
def test_instance_admin_displays(automation_content, rf, admin_user):
    """is_success / data_display / error_message_display helpers."""
    admin_instance = AutomationInstanceAdmin(AutomationInstance, admin_site=None)

    instance = AutomationInstance.objects.create(automation_content=automation_content, data=[{"x": 1}])

    # No actions at all -> success (nothing failed, nothing running)
    assert admin_instance.is_success(instance) is True

    running = AutomationAction.objects.create(automation_instance=instance, plugin_ptr=uuid.uuid4())
    assert admin_instance.is_success(instance) is None  # still running

    running.state = FAILED
    running.result = {"error": "boom", "traceback": "tb..."}
    running.save()
    assert admin_instance.is_success(instance) is False

    assert "&quot;x&quot;: 1" in admin_instance.data_display(instance)
    errors = admin_instance.error_message_display(instance)
    assert "boom" in errors and "tb..." in errors

    empty = AutomationInstance.objects.create(automation_content=automation_content, data=[])
    assert admin_instance.data_display(empty) == "-"
    assert admin_instance.error_message_display(empty) == "-"

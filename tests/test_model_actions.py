"""Tests for the Django model CRUD actions."""

import pytest

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation.actions.model_actions import (
    CreateModelActionModel,
    QueryModelActionModel,
    UpdateModelActionModel,
    get_allowed_model,
)
from djangocms_automation.instances import COMPLETED, FAILED, AutomationAction
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger

User = get_user_model()


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Model Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Model automation content",
    )


@pytest.fixture
def run_setup(automation_content, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    settings.AUTOMATION_ALLOWED_MODELS = ["auth.User"]

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


def _add_action(placeholder, plugin_type, model_cls, config, settings):
    plugin = add_plugin(placeholder=placeholder, plugin_type=plugin_type, language=settings.LANGUAGE_CODE)
    model = model_cls.objects.get(pk=plugin.pk)
    model.config = config
    model.save()
    return model


@pytest.mark.django_db
def test_allowed_models_gate_denies_by_default(settings):
    settings.AUTOMATION_ALLOWED_MODELS = []
    with pytest.raises(ValueError, match="not allowed"):
        get_allowed_model("auth.User")


@pytest.mark.django_db
def test_create_model_action(run_setup, settings):
    trigger, placeholder = run_setup
    _add_action(
        placeholder,
        "CreateModelAction",
        CreateModelActionModel,
        {"model": "auth.User", "field_mapping": {"username": "username", "email": "email"}},
        settings,
    )

    trigger.trigger_execution(
        data=[
            {"username": "alice", "email": "alice@example.com"},
            {"username": "bob", "email": "bob@example.com"},
        ],
        start=True,
    )

    assert User.objects.filter(username__in=["alice", "bob"]).count() == 2
    instance = trigger.automation_content.automationinstance_set.first()
    action = AutomationAction.objects.get(automation_instance=instance)
    assert action.state == COMPLETED
    assert all("_created_id" in row for row in action.result)


@pytest.mark.django_db
def test_create_model_action_disallowed_model_fails(run_setup, settings):
    trigger, placeholder = run_setup
    _add_action(
        placeholder,
        "CreateModelAction",
        CreateModelActionModel,
        {"model": "auth.Group", "field_mapping": {"name": "name"}},
        settings,
    )
    trigger.trigger_execution(data=[{"name": "g"}], start=True)
    instance = trigger.automation_content.automationinstance_set.first()
    action = AutomationAction.objects.get(automation_instance=instance)
    assert action.state == FAILED
    assert "not allowed" in action.result["error"]


@pytest.mark.django_db
def test_update_model_action(run_setup, settings):
    trigger, placeholder = run_setup
    User.objects.create_user(username="carol", email="old@example.com")
    _add_action(
        placeholder,
        "UpdateModelAction",
        UpdateModelActionModel,
        {
            "model": "auth.User",
            "filters": {"username": "username"},
            "field_mapping": {"email": "new_email"},
        },
        settings,
    )

    trigger.trigger_execution(data=[{"username": "carol", "new_email": "new@example.com"}], start=True)

    assert User.objects.get(username="carol").email == "new@example.com"
    instance = trigger.automation_content.automationinstance_set.first()
    action = AutomationAction.objects.get(automation_instance=instance)
    assert action.state == COMPLETED
    assert action.result[0]["_updated"] == 1


@pytest.mark.django_db
def test_update_without_filters_fails(run_setup, settings):
    trigger, placeholder = run_setup
    _add_action(
        placeholder,
        "UpdateModelAction",
        UpdateModelActionModel,
        {"model": "auth.User", "filters": {}, "field_mapping": {"email": "'x@example.com'"}},
        settings,
    )
    trigger.trigger_execution(data=[{}], start=True)
    action = AutomationAction.objects.get(
        automation_instance=trigger.automation_content.automationinstance_set.first()
    )
    assert action.state == FAILED


@pytest.mark.django_db
def test_query_model_action(run_setup, settings, admin_user):
    trigger, placeholder = run_setup
    User.objects.create_user(username="dave", email="dave@example.com")
    User.objects.create_user(username="erin", email="erin@example.com")
    _add_action(
        placeholder,
        "QueryModelAction",
        QueryModelActionModel,
        {
            "model": "auth.User",
            "filters": {"email__endswith": "'@example.com'"},
            "fields": "username, email",
            "order_by": "username",
            "limit": 10,
        },
        settings,
    )

    trigger.trigger_execution(data=[], start=True)

    action = AutomationAction.objects.get(
        automation_instance=trigger.automation_content.automationinstance_set.first()
    )
    assert action.state == COMPLETED
    usernames = [row["username"] for row in action.result]
    assert usernames == sorted(usernames)
    assert {"pk", "username", "email"} <= set(action.result[0])

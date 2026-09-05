"""Trigger intake creates one instance with a frozen executable definition."""

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from django.contrib.contenttypes.models import ContentType

from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def trigger(db, admin_user):
    automation = Automation.objects.create(name="Test Automation", is_active=True)
    content = AutomationContent.objects.with_user(admin_user).create(automation=automation)
    return AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")


def prepare(trigger, settings):
    placeholder = Placeholder.objects.create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=trigger.automation_content_id,
        slot=trigger.slot,
    )
    return add_plugin(placeholder, "ActionPlugin", settings.LANGUAGE_CODE)


def test_trigger_execution_creates_instance_and_action(trigger, settings):
    plugin = prepare(trigger, settings)
    data = {"user_id": 123, "action": "test"}
    instance = trigger.trigger_execution(data=data, start=False)
    assert instance.data == instance.initial_data == data
    assert instance.key
    assert instance.definition["trigger"]["slot"] == "start"
    action = instance.automationaction_set.get()
    assert action.previous is None
    assert action.plugin_ptr == plugin.uuid
    assert action.finished is None


def test_trigger_execution_with_no_data(trigger, settings):
    prepare(trigger, settings)
    instance = trigger.trigger_execution(start=False)
    assert instance.data == instance.initial_data == {}


def test_trigger_execution_multiple_calls_create_multiple_instances(trigger, settings):
    prepare(trigger, settings)
    instances = [trigger.trigger_execution(data={"run": number}, start=False) for number in (1, 2, 3)]
    assert [instance.data for instance in instances] == [{"run": 1}, {"run": 2}, {"run": 3}]
    assert len({instance.pk for instance in instances}) == 3


def test_trigger_execution_uses_correct_slot(trigger, settings):
    prepare(trigger, settings)
    custom = AutomationTrigger.objects.create(
        automation_content=trigger.automation_content,
        slot="custom_slot",
        type="click",
        position=1,
    )
    plugin = prepare(custom, settings)
    instance = custom.trigger_execution(start=False)
    assert instance.definition["trigger"]["slot"] == "custom_slot"
    assert instance.automationaction_set.get().plugin_ptr == plugin.uuid

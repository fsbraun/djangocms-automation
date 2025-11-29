"""Tests for AutomationTrigger.trigger_execution method."""

import uuid
from types import SimpleNamespace

import pytest
from django.conf import settings

from djangocms_automation.models import (
    Automation,
    AutomationContent,
    AutomationTrigger,
)


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Test Automation", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Test automation content"
    )


@pytest.fixture
def trigger(automation_content):
    return AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="start",
        type="click",
        position=0,
    )


class DummyPlugin:
    def __init__(self, plugin_uuid):
        self.uuid = plugin_uuid
        self.prepare_called = False
        self.prepare_action = None

    def prepare_execution(self, action):
        self.prepare_called = True
        self.prepare_action = action

    # Mimic CMSPlugin API: return (instance, model_class)
    def get_plugin_instance(self):
        return self, type(self)


@pytest.mark.django_db
class TestAutomationTriggerExecution:

    def test_trigger_execution_creates_instance_and_action(self, trigger, monkeypatch):
        """Test that trigger_execution creates AutomationInstance and AutomationAction."""
        # Setup
        plugin_uuid = uuid.uuid4()
        dummy_plugin = DummyPlugin(plugin_uuid)

        # Mock Placeholder.objects.get_for_obj
        mock_placeholder = SimpleNamespace(
            slot=trigger.slot,
            get_plugins=lambda: SimpleNamespace(first=lambda: dummy_plugin)
        )

        def mock_get_for_obj(obj):
            return SimpleNamespace(get=lambda slot: mock_placeholder)

        from cms.models import Placeholder
        monkeypatch.setattr(Placeholder.objects, "get_for_obj", mock_get_for_obj)

        test_data = {"user_id": 123, "action": "test"}

        # Act
        trigger.trigger_execution(data=test_data, start=False)

        # Assert - AutomationInstance created
        from djangocms_automation.instances import AutomationInstance
        instance = AutomationInstance.objects.filter(
            automation_content=trigger.automation_content
        ).first()

        assert instance is not None
        assert instance.data == test_data
        assert instance.initial_data == test_data
        assert instance.key is not None

        # Assert - AutomationAction created
        from djangocms_automation.instances import AutomationAction
        action = AutomationAction.objects.filter(
            automation_instance=instance
        ).first()

        assert action is not None
        assert action.previous is None
        assert action.plugin_ptr == plugin_uuid
        assert action.finished is None


    def test_trigger_execution_with_no_data(self, trigger, monkeypatch):
        """Test that trigger_execution works with data=None, defaulting to empty dict."""
        plugin_uuid = str(uuid.uuid4())
        dummy_plugin = DummyPlugin(plugin_uuid)

        mock_placeholder = SimpleNamespace(
            slot=trigger.slot,
            get_plugins=lambda: SimpleNamespace(first=lambda: dummy_plugin)
        )

        def mock_get_for_obj(obj):
            return SimpleNamespace(get=lambda slot: mock_placeholder)

        from cms.models import Placeholder
        monkeypatch.setattr(Placeholder.objects, "get_for_obj", mock_get_for_obj)

        # Act - call with no data argument
        trigger.trigger_execution(start=False)

        # Assert
        from djangocms_automation.instances import AutomationInstance
        instance = AutomationInstance.objects.filter(
            automation_content=trigger.automation_content
        ).first()

        assert instance is not None
        assert instance.data == {}
        assert instance.initial_data == {}

    def test_trigger_execution_multiple_calls_create_multiple_instances(self, trigger, monkeypatch):
        """Test that multiple trigger_execution calls create separate instances."""
        plugin_uuid = str(uuid.uuid4())
        dummy_plugin = DummyPlugin(plugin_uuid)

        mock_placeholder = SimpleNamespace(
            slot=trigger.slot,
            get_plugins=lambda: SimpleNamespace(first=lambda: dummy_plugin)
        )

        def mock_get_for_obj(obj):
            return SimpleNamespace(get=lambda slot: mock_placeholder)

        from cms.models import Placeholder
        monkeypatch.setattr(Placeholder.objects, "get_for_obj", mock_get_for_obj)

        # Act - trigger multiple times
        trigger.trigger_execution(data={"run": 1}, start=False)
        trigger.trigger_execution(data={"run": 2}, start=False)
        trigger.trigger_execution(data={"run": 3}, start=False)

        # Assert - three separate instances
        from djangocms_automation.instances import AutomationInstance
        instances = AutomationInstance.objects.filter(
            automation_content=trigger.automation_content
        )

        assert instances.count() == 3
        assert instances[0].data == {"run": 1}
        assert instances[1].data == {"run": 2}
        assert instances[2].data == {"run": 3}

        # Each instance has unique id
        ids = [inst.id for inst in instances]
        assert len(ids) == len(set(ids))  # all unique IDs

    def test_trigger_execution_uses_correct_slot(self, automation_content, monkeypatch):
        """Test that trigger_execution gets placeholder for correct slot."""
        # Create trigger with custom slot
        custom_trigger = AutomationTrigger.objects.create(
            automation_content=automation_content,
            slot="custom_slot",
            type="timer",
            position=1,
        )

        plugin_uuid = str(uuid.uuid4())
        dummy_plugin = DummyPlugin(plugin_uuid)

        slot_requests = []

        def mock_get(slot):
            slot_requests.append(slot)
            return SimpleNamespace(
                slot=slot,
                get_plugins=lambda: SimpleNamespace(first=lambda: dummy_plugin)
            )

        def mock_get_for_obj(obj):
            return SimpleNamespace(get=mock_get)

        from cms.models import Placeholder
        monkeypatch.setattr(Placeholder.objects, "get_for_obj", mock_get_for_obj)

        # Act
        custom_trigger.trigger_execution(start=False)

        # Assert - requested correct slot
        assert "custom_slot" in slot_requests

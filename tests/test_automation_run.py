"""End-to-end test: run an automation via AutomationTrigger and execute actions."""

import pytest

from cms.api import add_plugin
from cms.models import Placeholder
from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool

from djangocms_automation.instances import AutomationAction
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger, BaseActionPluginModel
from djangocms_automation.instances import COMPLETED


class DummyActionPluginModel(BaseActionPluginModel):
    """Real CMSPlugin-based dummy action model for testing."""

    class Meta:
        app_label = "djangocms_automation"

    def execute(self, action: AutomationAction, data=None, single_step=False, plugin_dict=None):
        """Execute the action and return status."""
        # Simple successful execution with output
        output = {"input": data, "plugin_id": str(self.uuid)}
        return COMPLETED, output


@plugin_pool.register_plugin
class DummyActionPlugin(CMSPluginBase):
    """CMS Plugin wrapper for DummyActionPluginModel."""

    model = DummyActionPluginModel
    name = "Dummy Action Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Run Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Run automation content",
    )


@pytest.mark.django_db
def test_run_automation_through_chain(automation_content, admin_user, settings):
    # Use immediate backend for synchronous task execution in tests
    settings.TASKS = {
        "default": {
            "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
        }
    }

    # Create a Trigger on slot 'start'
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="start",
        type="click",
        position=0,
    )

    # Get or create placeholder for the start slot
    from django.contrib.contenttypes.models import ContentType

    content_type = ContentType.objects.get_for_model(AutomationContent)
    placeholder = Placeholder.objects.get_or_create(
        content_type=content_type,
        object_id=automation_content.pk,
        slot="start",
    )[0]

    # Add three dummy action plugins in a chain
    p1 = add_plugin(
        placeholder=placeholder,
        plugin_type="DummyActionPlugin",
        language=settings.LANGUAGE_CODE,
    )
    p1 = DummyActionPluginModel.objects.get(pk=p1.pk)

    p2 = add_plugin(
        placeholder=placeholder,
        plugin_type="DummyActionPlugin",
        language=settings.LANGUAGE_CODE,
        position="first-child",
    )
    p2 = DummyActionPluginModel.objects.get(pk=p2.pk)

    p3 = add_plugin(
        placeholder=placeholder,
        plugin_type="DummyActionPlugin",
        language=settings.LANGUAGE_CODE,
        position="last-child",
    )
    p3 = DummyActionPluginModel.objects.get(pk=p3.pk)

    # Trigger the automation - this will use the task framework
    trigger.trigger_execution(data={"init": True}, start=True)

    # Get the created instance and verify actions were created and executed
    instance = automation_content.automationinstance_set.first()
    assert instance is not None
    assert instance.data == {"init": True}
    assert instance.initial_data == {"init": True}

    # Check that actions were created through the chain
    all_actions = AutomationAction.objects.filter(automation_instance=instance).order_by("created")

    # With immediate backend, tasks execute synchronously
    # We should have 3 actions (one for each plugin in the chain)
    assert all_actions.count() == 3

    # Get the first plugin from placeholder to compare UUIDs
    first_cms_plugin = placeholder.get_plugins().first()
    first_plugin, _ = first_cms_plugin.get_plugin_instance()
    first_action = all_actions.first()
    assert first_action.plugin_ptr == first_plugin.uuid

    # Verify all actions were executed successfully
    for action in all_actions:
        assert action.state == COMPLETED
        assert action.finished is not None
        assert "input" in action.result
        assert "plugin_id" in action.result


@pytest.mark.django_db
def test_run_automation_with_split_children_executes_first_branch_only(automation_content, admin_user, settings):
    """Create a split with two path children, each containing a DummyAction.

    This verifies that AutomationSplit creates actions for all AutomationPath children,
    and the automation continues through the paths.
    """
    settings.TASKS = {
        "default": {
            "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
        }
    }

    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="start",
        type="click",
        position=0,
    )

    from django.contrib.contenttypes.models import ContentType

    content_type = ContentType.objects.get_for_model(AutomationContent)
    placeholder = Placeholder.objects.get_or_create(
        content_type=content_type,
        object_id=automation_content.pk,
        slot="start",
    )[0]

    # Create initial action plugin
    add_plugin(
        placeholder=placeholder,
        plugin_type="DummyActionPlugin",
        language=settings.LANGUAGE_CODE,
    )

    # Create split plugin as child
    split = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationSplit",
        language=settings.LANGUAGE_CODE,
        position="last-child",
    )

    # Create two path children under split
    path1 = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationPath",
        language=settings.LANGUAGE_CODE,
        target=split,
    )

    path2 = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationPath",
        language=settings.LANGUAGE_CODE,
        target=split,
    )

    # Add action plugins under each path
    add_plugin(
        placeholder=placeholder,
        plugin_type="DummyActionPlugin",
        language=settings.LANGUAGE_CODE,
        target=path1,
    )

    add_plugin(
        placeholder=placeholder,
        plugin_type="DummyActionPlugin",
        language=settings.LANGUAGE_CODE,
        target=path2,
    )

    # Execute
    trigger.trigger_execution(data={"split": True}, start=True)

    instance = automation_content.automationinstance_set.first()
    assert instance is not None

    actions = AutomationAction.objects.filter(automation_instance=instance).order_by("created")
    # Should have actions for: initial action, split (creates 2 path actions), and 2 nested actions
    assert actions.count() == 4
    assert all(action.state == COMPLETED for action in actions)

    # All executed actions should be completed
    for a in actions:
        assert a.state == COMPLETED

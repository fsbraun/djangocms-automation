"""Unit tests for fan-out node states that the synchronous (immediate)
backend cannot reach end-to-end: straggler branches and failed children on
revival, plus the editor validation ``messages()`` helpers."""

import pytest

from django.contrib.contenttypes.models import ContentType

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation import engine
from djangocms_automation.instances import (
    COMPLETED,
    FAILED,
    RUNNING,
    WAITING,
    AutomationAction,
    AutomationInstance,
)
from djangocms_automation.models import (
    Automation,
    AutomationContent,
    ConditionalPluginModel,
    SplitPluginModel,
)


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Node State Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Node state automation content",
    )


@pytest.fixture
def placeholder(automation_content):
    return Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]


def _make_split(placeholder, settings, paths=2):
    split = add_plugin(placeholder=placeholder, plugin_type="AutomationSplit", language=settings.LANGUAGE_CODE)
    branch_plugins = []
    for _i in range(paths):
        path = add_plugin(
            placeholder=placeholder, plugin_type="AutomationPath", language=settings.LANGUAGE_CODE, target=split
        )
        branch_plugins.append(
            add_plugin(
                placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, target=path
            )
        )
    return split, branch_plugins


@pytest.mark.django_db
def test_split_revival_with_straggler_keeps_waiting(placeholder, automation_content, settings):
    split, branch_plugins = _make_split(placeholder, settings)
    plugin_map = engine.build_plugin_map(automation_content.pk)
    split_plugin = SplitPluginModel.objects.get(pk=split.pk)
    split_node = plugin_map[split_plugin.uuid]

    instance = AutomationInstance.objects.create(automation_content=automation_content)
    split_action = AutomationAction.objects.create(
        automation_instance=instance, plugin_ptr=split_plugin.uuid, state=WAITING
    )
    ends = [plugin_map[p.uuid].uuid for p in [type(bp).objects.get(pk=bp.pk) for bp in branch_plugins]]
    # One branch finished, one still running -> the split keeps WAITING.
    AutomationAction.objects.create(
        automation_instance=instance,
        plugin_ptr=ends[0],
        parent=split_action,
        state=COMPLETED,
        finished="2026-01-01T00:00:00+00:00",
        result=[{"done": 1}],
    )
    straggler = AutomationAction.objects.create(
        automation_instance=instance, plugin_ptr=ends[1], parent=split_action, state=RUNNING
    )

    state, output = split_node.execute(split_action, [], plugin_dict=plugin_map)
    assert state == WAITING
    assert output == {}

    # Straggler finishes -> the join completes and merges branch outputs.
    AutomationAction.objects.filter(pk=straggler.pk).update(
        state=COMPLETED, finished="2026-01-01T00:01:00+00:00", result=[{"done": 2}]
    )
    state, output = split_node.execute(split_action, [], plugin_dict=plugin_map)
    assert state == COMPLETED
    assert sorted(row["done"] for row in output) == [1, 2]
    assert split_action.message == "Joined"


@pytest.mark.django_db
def test_split_revival_with_failed_child_fails(placeholder, automation_content, settings):
    split, branch_plugins = _make_split(placeholder, settings)
    plugin_map = engine.build_plugin_map(automation_content.pk)
    split_plugin = SplitPluginModel.objects.get(pk=split.pk)
    split_node = plugin_map[split_plugin.uuid]

    instance = AutomationInstance.objects.create(automation_content=automation_content)
    split_action = AutomationAction.objects.create(
        automation_instance=instance, plugin_ptr=split_plugin.uuid, state=WAITING
    )
    AutomationAction.objects.create(
        automation_instance=instance, plugin_ptr=split_plugin.uuid, parent=split_action, state=FAILED
    )

    state, output = split_node.execute(split_action, [], plugin_dict=plugin_map)
    assert state == FAILED
    assert "failed" in output["error"]


@pytest.mark.django_db
def test_split_without_paths_passes_through(placeholder, automation_content, settings):
    split = add_plugin(placeholder=placeholder, plugin_type="AutomationSplit", language=settings.LANGUAGE_CODE)
    plugin_map = engine.build_plugin_map(automation_content.pk)
    split_plugin = SplitPluginModel.objects.get(pk=split.pk)
    split_node = plugin_map[split_plugin.uuid]

    instance = AutomationInstance.objects.create(automation_content=automation_content)
    action = AutomationAction.objects.create(automation_instance=instance, plugin_ptr=split_plugin.uuid)

    state, output = split_node.execute(action, [{"x": 1}], plugin_dict=plugin_map)
    assert state == COMPLETED
    assert output == [{"x": 1}]


def _make_conditional(placeholder, settings, condition=None):
    conditional = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationIf",
        language=settings.LANGUAGE_CODE,
        condition=condition or {},
    )
    then_branch = add_plugin(
        placeholder=placeholder, plugin_type="ThenPlugin", language=settings.LANGUAGE_CODE, target=conditional
    )
    branch_action = add_plugin(
        placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, target=then_branch
    )
    return conditional, branch_action


@pytest.mark.django_db
def test_conditional_revival_straggler_and_failure(placeholder, automation_content, settings):
    conditional, branch_action_plugin = _make_conditional(placeholder, settings)
    plugin_map = engine.build_plugin_map(automation_content.pk)
    cond_model = ConditionalPluginModel.objects.get(pk=conditional.pk)
    cond_node = plugin_map[cond_model.uuid]

    instance = AutomationInstance.objects.create(automation_content=automation_content)
    cond_action = AutomationAction.objects.create(
        automation_instance=instance,
        plugin_ptr=cond_model.uuid,
        state=WAITING,
        result={"condition": True},
    )
    child = AutomationAction.objects.create(
        automation_instance=instance, plugin_ptr=cond_model.uuid, parent=cond_action, state=RUNNING
    )

    # Child still running -> keep waiting.
    state, output = cond_node.execute(cond_action, [{"in": 1}], plugin_dict=plugin_map)
    assert state == WAITING

    # Child failed -> conditional reports failure.
    AutomationAction.objects.filter(pk=child.pk).update(state=FAILED)
    state, output = cond_node.execute(cond_action, [{"in": 1}], plugin_dict=plugin_map)
    assert state == FAILED

    # Child completed -> conditional completes with the branch end's output.
    from djangocms_automation.models import BaseActionPluginModel

    end_uuid = BaseActionPluginModel.objects.get(pk=branch_action_plugin.pk).uuid
    AutomationAction.objects.filter(pk=child.pk).update(
        state=COMPLETED,
        finished="2026-01-01T00:00:00+00:00",
        plugin_ptr=end_uuid,
        result=[{"branch": "output"}],
    )
    state, output = cond_node.execute(cond_action, [{"in": 1}], plugin_dict=plugin_map)
    assert state == COMPLETED
    assert output == [{"branch": "output"}]


# ---------------------------------------------------------------------------
# Editor validation messages
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_conditional_messages_flag_missing_and_duplicate_branches(placeholder, automation_content, settings):
    conditional = add_plugin(
        placeholder=placeholder, plugin_type="AutomationIf", language=settings.LANGUAGE_CODE, condition={}
    )
    plugin_map = engine.build_plugin_map(automation_content.pk)
    cond_model = ConditionalPluginModel.objects.get(pk=conditional.pk)
    node = plugin_map[cond_model.uuid]

    # No branches at all: both warnings.
    messages = node.messages()
    assert len(messages) == 2

    # Duplicate Yes branches: the multiple-channels warning appears.
    add_plugin(placeholder=placeholder, plugin_type="ThenPlugin", language=settings.LANGUAGE_CODE, target=conditional)
    add_plugin(placeholder=placeholder, plugin_type="ThenPlugin", language=settings.LANGUAGE_CODE, target=conditional)
    add_plugin(placeholder=placeholder, plugin_type="ElsePlugin", language=settings.LANGUAGE_CODE, target=conditional)
    plugin_map = engine.build_plugin_map(automation_content.pk)
    node = plugin_map[cond_model.uuid]
    messages = node.messages()
    assert any("cannot be defined more than once" in str(m) for m in messages)


@pytest.mark.django_db
def test_split_messages_flag_missing_paths(placeholder, automation_content, settings):
    split = add_plugin(placeholder=placeholder, plugin_type="AutomationSplit", language=settings.LANGUAGE_CODE)
    plugin_map = engine.build_plugin_map(automation_content.pk)
    node = plugin_map[SplitPluginModel.objects.get(pk=split.pk).uuid]
    assert len(node.messages()) == 1

    add_plugin(placeholder=placeholder, plugin_type="AutomationPath", language=settings.LANGUAGE_CODE, target=split)
    plugin_map = engine.build_plugin_map(automation_content.pk)
    node = plugin_map[SplitPluginModel.objects.get(pk=split.pk).uuid]
    assert node.messages() == []

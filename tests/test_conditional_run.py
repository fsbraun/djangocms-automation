"""End-to-end tests for conditional (If/Then/Else) branching."""

import pytest

from django.contrib.contenttypes.models import ContentType

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation.instances import COMPLETED, AutomationAction
from djangocms_automation.models import (
    Automation,
    AutomationContent,
    AutomationTrigger,
    ConditionalPluginModel,
)


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Conditional Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Conditional automation content",
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


def _build_conditional(placeholder, settings, condition, with_else=True, with_follow_up=True):
    """Placeholder layout: [If [Then [Action] ] [Else [Action] ] ] [Action]."""
    conditional = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationIf",
        language=settings.LANGUAGE_CODE,
        condition=condition,
    )
    then_branch = add_plugin(
        placeholder=placeholder, plugin_type="ThenPlugin", language=settings.LANGUAGE_CODE, target=conditional
    )
    then_action = add_plugin(
        placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, target=then_branch
    )
    else_action = None
    if with_else:
        else_branch = add_plugin(
            placeholder=placeholder, plugin_type="ElsePlugin", language=settings.LANGUAGE_CODE, target=conditional
        )
        else_action = add_plugin(
            placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, target=else_branch
        )
    follow_up = None
    if with_follow_up:
        follow_up = add_plugin(
            placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, position="last-child"
        )
    return conditional, then_action, else_action, follow_up


CONDITION = {"logic": "and", "conditions": [{"field": "score", "operator": ">", "value": "10"}]}


@pytest.mark.django_db
@pytest.mark.parametrize("score, expect_condition", [(50, True), (5, False)])
def test_conditional_routes_matching_branch(run_setup, settings, score, expect_condition):
    trigger, placeholder = run_setup
    conditional, then_action, else_action, follow_up = _build_conditional(placeholder, settings, CONDITION)

    trigger.trigger_execution(data=[{"score": score}], start=True)

    instance = trigger.automation_content.automationinstance_set.first()
    actions = AutomationAction.objects.filter(automation_instance=instance)
    # trigger-side: conditional + one branch action + follow-up
    assert actions.count() == 3
    assert all(a.state == COMPLETED for a in actions)

    executed_uuids = {a.plugin_ptr for a in actions}
    # Resolve the branch action plugin uuids via their downcast models
    from djangocms_automation.models import BaseActionPluginModel

    then_uuid = BaseActionPluginModel.objects.get(pk=then_action.pk).uuid
    else_uuid = BaseActionPluginModel.objects.get(pk=else_action.pk).uuid
    if expect_condition:
        assert then_uuid in executed_uuids
        assert else_uuid not in executed_uuids
    else:
        assert else_uuid in executed_uuids
        assert then_uuid not in executed_uuids

    # The conditional action recorded its outcome and the flow resumed after it.
    conditional_model = ConditionalPluginModel.objects.get(pk=conditional.pk)
    conditional_action = actions.get(plugin_ptr=conditional_model.uuid)
    assert conditional_action.state == COMPLETED
    follow_up_uuid = BaseActionPluginModel.objects.get(pk=follow_up.pk).uuid
    assert follow_up_uuid in executed_uuids
    instance.refresh_from_db()
    assert instance.status == COMPLETED
    # Branch output (pass-through of the input rows) flowed to the follow-up.
    assert instance.data == [{"score": score}]


@pytest.mark.django_db
def test_conditional_missing_branch_passes_through(run_setup, settings):
    trigger, placeholder = run_setup
    # No Else branch; condition is false -> pass-through to follow-up.
    _build_conditional(placeholder, settings, CONDITION, with_else=False)

    trigger.trigger_execution(data=[{"score": 1}], start=True)

    instance = trigger.automation_content.automationinstance_set.first()
    actions = AutomationAction.objects.filter(automation_instance=instance)
    # conditional (pass-through) + follow-up only
    assert actions.count() == 2
    assert all(a.state == COMPLETED for a in actions)
    instance.refresh_from_db()
    assert instance.status == COMPLETED
    assert instance.data == [{"score": 1}]


@pytest.mark.django_db
def test_conditional_failing_branch_fails_conditional_and_instance(run_setup, settings):
    """A failure inside the chosen branch fails the conditional and the run."""
    from djangocms_automation.actions.mail import MailActionPluginModel
    from djangocms_automation.instances import FAILED

    trigger, placeholder = run_setup
    conditional = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationIf",
        language=settings.LANGUAGE_CODE,
        condition=CONDITION,
    )
    then_branch = add_plugin(
        placeholder=placeholder, plugin_type="ThenPlugin", language=settings.LANGUAGE_CODE, target=conditional
    )
    failing = add_plugin(
        placeholder=placeholder, plugin_type="MailAction", language=settings.LANGUAGE_CODE, target=then_branch
    )
    failing_model = MailActionPluginModel.objects.get(pk=failing.pk)
    failing_model.config = {"subject": "'s'", "body": "b", "recipient_email": "missing"}
    failing_model.save()

    trigger.trigger_execution(data=[{"score": 99}], start=True)  # condition true -> Then branch

    instance = trigger.automation_content.automationinstance_set.first()
    actions = AutomationAction.objects.filter(automation_instance=instance)
    conditional_model = ConditionalPluginModel.objects.get(pk=conditional.pk)
    conditional_action = actions.get(plugin_ptr=conditional_model.uuid)
    branch_action = actions.exclude(pk=conditional_action.pk).get()

    assert branch_action.state == FAILED
    assert conditional_action.state == FAILED
    assert conditional_action.message == "Branch failed"
    instance.refresh_from_db()
    assert instance.status == FAILED
    assert instance.finished is not None

"""Tests for the execution engine (claiming, failure propagation, pause/revive)."""

import datetime
import uuid

import pytest

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.utils.timezone import now

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation import engine
from djangocms_automation.instances import (
    COMPLETED,
    FAILED,
    PENDING,
    RUNNING,
    AutomationAction,
    AutomationInstance,
)
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Engine Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Engine automation content",
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


def test_normalize_rows():
    assert engine.normalize_rows(None) == []
    assert engine.normalize_rows([]) == []
    assert engine.normalize_rows({}) == []
    assert engine.normalize_rows({"a": 1}) == [{"a": 1}]
    assert engine.normalize_rows([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert engine.normalize_rows("x") == [{"value": "x"}]


@pytest.mark.django_db
def test_claim_action_is_idempotent(automation_content):
    instance = AutomationInstance.objects.create(automation_content=automation_content)
    action = AutomationAction.objects.create(automation_instance=instance, plugin_ptr=uuid.uuid4())

    claimed = engine.claim_action(action.pk)
    assert claimed is not None
    assert claimed.state == RUNNING
    # A second claim (double enqueue) is a no-op.
    assert engine.claim_action(action.pk) is None


@pytest.mark.django_db
def test_missing_plugin_fails_action_not_crash(run_setup):
    trigger, placeholder = run_setup
    instance = AutomationInstance.objects.create(automation_content=trigger.automation_content)
    action = AutomationAction.objects.create(
        automation_instance=instance,
        plugin_ptr=uuid.uuid4(),  # does not exist in the plugin tree
    )

    engine.run_action(action.pk)

    action.refresh_from_db()
    assert action.state == FAILED
    assert "no longer exists" in action.result["error"]
    instance.refresh_from_db()
    assert instance.status == FAILED
    assert instance.finished is not None


@pytest.mark.django_db
def test_split_branch_failure_fails_split_and_instance(run_setup, settings):
    """A failing branch fails the split parent and the instance (fail-fast)."""
    trigger, placeholder = run_setup

    split = add_plugin(placeholder=placeholder, plugin_type="AutomationSplit", language=settings.LANGUAGE_CODE)
    path1 = add_plugin(
        placeholder=placeholder, plugin_type="AutomationPath", language=settings.LANGUAGE_CODE, target=split
    )
    path2 = add_plugin(
        placeholder=placeholder, plugin_type="AutomationPath", language=settings.LANGUAGE_CODE, target=split
    )
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, target=path1)
    # MailAction with unresolvable recipient -> fails at runtime.
    failing = add_plugin(
        placeholder=placeholder, plugin_type="MailAction", language=settings.LANGUAGE_CODE, target=path2
    )
    from djangocms_automation.actions.mail import MailActionPluginModel

    failing_model = MailActionPluginModel.objects.get(pk=failing.pk)
    failing_model.config = {"subject": "'s'", "body": "b", "recipient_email": "missing"}
    failing_model.save()

    trigger.trigger_execution(data=[{}], start=True)

    instance = trigger.automation_content.automationinstance_set.first()
    actions = AutomationAction.objects.filter(automation_instance=instance)
    split_action = actions.filter(parent__isnull=True).first()
    assert split_action.state == FAILED
    assert split_action.message == "Branch failed"
    assert actions.filter(state=FAILED).count() >= 2  # branch action + split
    instance.refresh_from_db()
    assert instance.status == FAILED
    assert instance.finished is not None


@pytest.mark.django_db
def test_split_join_merges_branch_outputs(run_setup, settings):
    trigger, placeholder = run_setup

    split = add_plugin(placeholder=placeholder, plugin_type="AutomationSplit", language=settings.LANGUAGE_CODE)
    for _i in range(2):
        path = add_plugin(
            placeholder=placeholder, plugin_type="AutomationPath", language=settings.LANGUAGE_CODE, target=split
        )
        add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, target=path)

    trigger.trigger_execution(data=[{"n": 1}], start=True)

    instance = trigger.automation_content.automationinstance_set.first()
    actions = AutomationAction.objects.filter(automation_instance=instance)
    split_action = actions.filter(parent__isnull=True).first()
    assert split_action.state == COMPLETED
    assert split_action.message == "Joined"
    # Both branches passed [{"n": 1}] through; the join concatenates them.
    assert split_action.result == [{"n": 1}, {"n": 1}]
    instance.refresh_from_db()
    assert instance.status == COMPLETED


@pytest.mark.django_db
def test_pause_and_revive_roundtrip(run_setup, settings):
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)

    # Create the instance without starting it, then pause the action.
    trigger.trigger_execution(data=[{"x": 1}], start=False)
    instance = trigger.automation_content.automationinstance_set.first()
    action = AutomationAction.objects.get(automation_instance=instance)
    engine.pause_action(action, until=now() + datetime.timedelta(hours=1), message="later")
    action.refresh_from_db()
    assert action.state == PENDING
    assert action.paused_until is not None

    # Not due yet: revive_pending skips it.
    assert engine.revive_pending() == 0

    # Due: revive executes it (immediate backend).
    AutomationAction.objects.filter(pk=action.pk).update(paused_until=now() - datetime.timedelta(seconds=1))
    assert engine.revive_pending() == 1
    action.refresh_from_db()
    assert action.state == COMPLETED


@pytest.mark.django_db
def test_runautomations_command_fires_due_timer(run_setup, settings, capsys):
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)
    # Rewire the trigger as a due one-shot timer.
    trigger.type = "timer"
    trigger.config = {"scheduled_at": (now() - datetime.timedelta(minutes=1)).isoformat()}
    trigger.save()

    call_command("runautomations")

    trigger.refresh_from_db()
    assert trigger.config.get("last_fired")
    assert trigger.config.get("fired_count") == 1
    instance = trigger.automation_content.automationinstance_set.first()
    assert instance is not None
    assert instance.status == COMPLETED

    # Second run: one-shot timer does not fire again.
    call_command("runautomations")
    trigger.refresh_from_db()
    assert trigger.config.get("fired_count") == 1
    assert trigger.automation_content.automationinstance_set.count() == 1


@pytest.mark.django_db
def test_recurring_timer_steps_forward(run_setup, settings):
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)
    start = now() - datetime.timedelta(hours=3)
    trigger.type = "timer"
    trigger.config = {
        "scheduled_at": start.isoformat(),
        "recurrence_frequency": "hourly",
        "recurrence_interval": 1,
    }
    trigger.save()

    # First call fires the initial occurrence; subsequent calls step forward.
    fired_total = 0
    for _i in range(5):
        fired_total += engine.fire_due_timers()
    assert fired_total == 4  # start, +1h, +2h, +3h — then caught up
    trigger.refresh_from_db()
    assert trigger.config["fired_count"] == 4

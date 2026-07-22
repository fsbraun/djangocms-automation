"""Tests for atomic automation action state transitions."""

import uuid

import pytest

from djangocms_automation.instances import COMPLETED, FAILED, PENDING, RUNNING, AutomationAction, AutomationInstance
from djangocms_automation.models import Automation, AutomationContent
from djangocms_automation.transitions import heartbeat_action, transition_action


@pytest.fixture
def action(db, admin_user):
    automation = Automation.objects.create(name="Transition Test", is_active=True)
    content = AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Transition test content",
    )
    instance = AutomationInstance.objects.create(automation_content=content)
    return AutomationAction.objects.create(automation_instance=instance, plugin_ptr=uuid.uuid4())


def test_claim_records_attempt_lease_and_event(action):
    claimed = transition_action(action.pk, RUNNING, allowed_from=(PENDING,), metadata={"source": "test"})

    assert claimed is not None
    assert claimed.attempt_count == 1
    assert claimed.started is not None
    assert claimed.heartbeat_at == claimed.started
    assert claimed.lease_id is not None
    event = claimed.events.get()
    assert (event.from_state, event.to_state, event.attempt) == (PENDING, RUNNING, 1)
    assert event.lease_id == claimed.lease_id
    assert event.metadata == {"source": "test"}


def test_duplicate_claim_is_noop(action):
    assert transition_action(action.pk, RUNNING, allowed_from=(PENDING,)) is not None
    assert transition_action(action.pk, RUNNING, allowed_from=(PENDING,)) is None

    action.refresh_from_db()
    assert action.attempt_count == 1
    assert action.events.count() == 1


def test_completion_records_result_and_finished_time(action):
    claimed = transition_action(action.pk, RUNNING, allowed_from=(PENDING,))
    completed = transition_action(
        claimed.pk,
        COMPLETED,
        allowed_from=(RUNNING,),
        result={"ok": True},
        message="Done",
    )

    assert completed.state == COMPLETED
    assert completed.result == {"ok": True}
    assert completed.message == "Done"
    assert completed.finished is not None
    assert list(completed.events.values_list("from_state", "to_state")) == [
        (PENDING, RUNNING),
        (RUNNING, COMPLETED),
    ]


def test_failure_records_structured_error(action):
    claimed = transition_action(action.pk, RUNNING, allowed_from=(PENDING,))
    failed = transition_action(
        claimed.pk,
        FAILED,
        allowed_from=(RUNNING,),
        error=ValueError("invalid payload"),
    )

    assert failed.error_type == "builtins.ValueError"
    assert failed.error_detail == "invalid payload"
    assert failed.finished is not None


def test_heartbeat_requires_current_lease(action):
    claimed = transition_action(action.pk, RUNNING, allowed_from=(PENDING,))
    previous_heartbeat = claimed.heartbeat_at

    assert heartbeat_action(claimed.pk, uuid.uuid4()) is False
    assert heartbeat_action(claimed.pk, claimed.lease_id) is True
    claimed.refresh_from_db()
    assert claimed.heartbeat_at >= previous_heartbeat

"""Atomic state transitions for automation actions."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from django.db import transaction
from django.utils.timezone import now

from .instances import (
    CANCELED,
    COMPLETED,
    FAILED,
    RUNNING,
    AutomationAction,
    AutomationActionEvent,
)
from .signals import action_transitioned

logger = logging.getLogger("djangocms_automation.engine")

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELED})
_UNSET = object()
MUTABLE_ACTION_FIELDS = frozenset(
    {
        "interaction_group_id",
        "interaction_permissions",
        "interaction_user_id",
        "requires_interaction",
        "input_data",
        "dead_lettered",
        "dead_lettered_at",
        "next_attempt_at",
        "paused_until",
        "timeout_seconds",
        "max_attempts",
        # Reopening a terminal action for replay has to clear its finish time,
        # or it stays invisible to the "any unfinished actions?" checks that
        # decide when an instance is complete.
        "finished",
    }
)


def _emit(action, from_state: str, to_state: str, metadata: dict) -> None:
    """Log the transition and notify observers, never raising into the engine."""
    logger.info(
        "automation.action.transition",
        extra={
            "automation_action_id": action.pk,
            "automation_instance_id": action.automation_instance_id,
            "plugin_ptr": str(action.plugin_ptr),
            "from_state": from_state,
            "to_state": to_state,
            "attempt": action.attempt_count,
            "re_entry": action.re_entry_count,
            "lease_id": str(action.lease_id) if action.lease_id else None,
            "duration": action.duration,
        },
    )
    try:
        action_transitioned.send(
            sender=AutomationAction,
            action=action,
            from_state=from_state,
            to_state=to_state,
            attempt=action.attempt_count,
            duration=action.duration,
            metadata=metadata,
        )
    except Exception:
        logger.exception("automation.signal.failed", extra={"automation_action_id": action.pk})


def transition_action(
    action_id: int,
    to_state: str,
    *,
    allowed_from: Iterable[str] | None = None,
    result=_UNSET,
    message: str | None = None,
    error: BaseException | None = None,
    metadata: dict | None = None,
    unfinished_only: bool = False,
    field_updates: dict | None = None,
    continuation: bool = False,
    require_lease: uuid.UUID | None = None,
) -> AutomationAction | None:
    """Atomically move an action to a new state and record an audit event.

    ``None`` is returned when the action no longer exists or its current state
    is not in ``allowed_from``. This makes duplicate task delivery a no-op.

    :param continuation: Mark the next claim as resuming work rather than
        re-attempting it. Set when waking a waiting node or reviving a paused
        action — neither is a failure, so neither may consume the retry budget.
    :param require_lease: Fence the transition to one execution attempt. Every
        worker-originated transition must pass the lease it claimed with,
        because state alone cannot tell two attempts apart: if worker A's lease
        expires, the scheduler recovers the action and worker B claims it, the
        action is ``RUNNING`` again — so an unfenced A, finishing late, would
        complete or fail *B's* attempt. A lease mismatch returns ``None`` and A's
        work is discarded, which is the correct outcome: it no longer owns the
        action.
    """
    allowed = set(allowed_from) if allowed_from is not None else None
    with transaction.atomic():
        action = AutomationAction.objects.select_for_update().filter(pk=action_id).first()
        if (
            action is None
            or (unfinished_only and action.finished is not None)
            or (allowed is not None and action.state not in allowed)
            or (require_lease is not None and action.lease_id != require_lease)
        ):
            return None

        from_state = action.state
        action.state = to_state
        update_fields = {"state"}

        if to_state == RUNNING and from_state != RUNNING:
            timestamp = now()
            # A waiting node resuming after its children finished is a re-entry,
            # not a retry: a split or agent that loops ten times has not failed
            # once, and must not consume the action's attempt budget.
            if action.resumed:
                action.re_entry_count += 1
                action.resumed = False
                update_fields.update({"re_entry_count", "resumed"})
            else:
                action.attempt_count += 1
                update_fields.add("attempt_count")
            action.started = timestamp
            action.heartbeat_at = timestamp
            action.finished = None
            action.next_attempt_at = None
            action.lease_id = uuid.uuid4()
            action.error_type = ""
            action.error_detail = ""
            update_fields.update(
                {
                    "started",
                    "heartbeat_at",
                    "finished",
                    "next_attempt_at",
                    "lease_id",
                    "error_type",
                    "error_detail",
                }
            )

        if continuation and not action.resumed:
            action.resumed = True
            update_fields.add("resumed")

        if to_state in TERMINAL_STATES:
            action.finished = now()
            update_fields.add("finished")

        if result is not _UNSET:
            action.result = result
            update_fields.add("result")
        if message is not None:
            action.message = message
            update_fields.add("message")
        if error is not None:
            error_class = type(error)
            action.error_type = f"{error_class.__module__}.{error_class.__qualname__}"
            action.error_detail = str(error)
            update_fields.update({"error_type", "error_detail"})
        for field, value in (field_updates or {}).items():
            if field not in MUTABLE_ACTION_FIELDS:
                raise ValueError(f"Unsupported action transition field: {field}")
            setattr(action, field, value)
            update_fields.add(field)

        action.save(update_fields=update_fields)
        AutomationActionEvent.objects.create(
            action=action,
            from_state=from_state,
            to_state=to_state,
            attempt=action.attempt_count,
            lease_id=action.lease_id,
            metadata=metadata or {},
        )
    _emit(action, from_state, to_state, metadata or {})
    return action


def heartbeat_action(action_id: int, lease_id: uuid.UUID) -> bool:
    """Refresh a running action lease if the caller still owns it."""
    return bool(
        AutomationAction.objects.filter(pk=action_id, state=RUNNING, lease_id=lease_id).update(heartbeat_at=now())
    )

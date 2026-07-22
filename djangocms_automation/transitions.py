"""Atomic state transitions for automation actions."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from django.db import transaction
from django.utils.timezone import now

from .instances import COMPLETED, FAILED, RUNNING, AutomationAction, AutomationActionEvent

TERMINAL_STATES = frozenset({COMPLETED, FAILED})
_UNSET = object()
MUTABLE_ACTION_FIELDS = frozenset(
    {
        "interaction_group_id",
        "interaction_permissions",
        "interaction_user_id",
        "requires_interaction",
    }
)


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
) -> AutomationAction | None:
    """Atomically move an action to a new state and record an audit event.

    ``None`` is returned when the action no longer exists or its current state
    is not in ``allowed_from``. This makes duplicate task delivery a no-op.
    """
    allowed = set(allowed_from) if allowed_from is not None else None
    with transaction.atomic():
        action = AutomationAction.objects.select_for_update().filter(pk=action_id).first()
        if (
            action is None
            or (unfinished_only and action.finished is not None)
            or (allowed is not None and action.state not in allowed)
        ):
            return None

        from_state = action.state
        action.state = to_state
        update_fields = {"state"}

        if to_state == RUNNING and from_state != RUNNING:
            timestamp = now()
            action.attempt_count += 1
            action.started = timestamp
            action.heartbeat_at = timestamp
            action.finished = None
            action.next_attempt_at = None
            action.lease_id = uuid.uuid4()
            action.error_type = ""
            action.error_detail = ""
            update_fields.update(
                {
                    "attempt_count",
                    "started",
                    "heartbeat_at",
                    "finished",
                    "next_attempt_at",
                    "lease_id",
                    "error_type",
                    "error_detail",
                }
            )

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
        return action


def heartbeat_action(action_id: int, lease_id: uuid.UUID) -> bool:
    """Refresh a running action lease if the caller still owns it."""
    return bool(
        AutomationAction.objects.filter(pk=action_id, state=RUNNING, lease_id=lease_id).update(heartbeat_at=now())
    )

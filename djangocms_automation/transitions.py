"""Atomic state transitions for automation actions."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from django.db import transaction
from django.utils.timezone import now

from .instances import (
    ALLOWED_INSTANCE_TRANSITIONS,
    ALLOWED_TRANSITIONS,
    CANCELED,
    COMPLETED,
    FAILED,
    RUNNING,
    AutomationAction,
    AutomationActionEvent,
    AutomationInstance,
    AutomationInstanceEvent,
)
from .signals import action_transitioned, instance_finished

logger = logging.getLogger("djangocms_automation.engine")

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELED})


class InvalidTransition(ValueError):
    """A state change that the action lifecycle does not permit.

    Raised rather than returned as ``None``, because this is a programming
    error, not a race: a refused-but-legal transition (wrong source state, lost
    lease, already finished) is normal and returns ``None`` quietly. Reaching
    here means the caller asked for something the lifecycle has no meaning for.
    """


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
    """Log the transition and notify observers, never raising into the engine.

    Deferred to commit by its callers: they wrap a transition together with the
    work that follows it, so a transition that has been written can still roll
    back. An observer told about a state change that never happened is worse
    than one told a moment late.
    """
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
        if to_state not in ALLOWED_TRANSITIONS.get(from_state, frozenset()):
            raise InvalidTransition(f"{from_state} -> {to_state} is not a legal action transition")

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
    transaction.on_commit(lambda: _emit(action, from_state, to_state, metadata or {}))
    return action


def heartbeat_action(action_id: int, lease_id: uuid.UUID) -> bool:
    """Refresh a running action lease if the caller still owns it."""
    return bool(
        AutomationAction.objects.filter(pk=action_id, state=RUNNING, lease_id=lease_id).update(heartbeat_at=now())
    )


def transition_instance(
    instance_id: int,
    to_status: str,
    *,
    allowed_from: Iterable[str] | None = None,
    unfinished_only: bool = False,
    metadata: dict | None = None,
) -> AutomationInstance | None:
    """Atomically move an automation instance to a new status and record it.

    The counterpart to :func:`transition_action`, and it exists for the same
    reason: a status written from three different places is a lifecycle nobody
    can read or audit. Every instance status change now goes through here, so
    each one is guarded by :data:`~djangocms_automation.instances.ALLOWED_INSTANCE_TRANSITIONS`,
    leaves an :class:`~djangocms_automation.instances.AutomationInstanceEvent`
    behind, and fires exactly one ``instance_finished`` signal when the run ends.

    Terminal statuses stamp ``finished``; moving back to ``RUNNING`` — which only
    replay does — clears it, so a reopened run is genuinely open again rather
    than merely relabelled.

    :returns: The updated instance, or ``None`` if the change was refused, which
        makes concurrent finishes idempotent in the same way as for actions.
    :raises InvalidTransition: If the lifecycle has no such edge.
    """
    allowed = set(allowed_from) if allowed_from is not None else None
    with transaction.atomic():
        instance = AutomationInstance.objects.select_for_update().filter(pk=instance_id).first()
        if (
            instance is None
            or (unfinished_only and instance.finished is not None)
            or (allowed is not None and instance.status not in allowed)
        ):
            return None

        from_status = instance.status
        if to_status not in ALLOWED_INSTANCE_TRANSITIONS.get(from_status, frozenset()):
            raise InvalidTransition(f"{from_status} -> {to_status} is not a legal instance transition")

        instance.status = to_status
        update_fields = {"status"}
        if to_status in TERMINAL_STATES:
            instance.finished = now()
            update_fields.add("finished")
        elif instance.finished is not None:
            instance.finished = None
            update_fields.add("finished")

        instance.save(update_fields=update_fields)
        AutomationInstanceEvent.objects.create(
            instance=instance,
            from_status=from_status,
            to_status=to_status,
            metadata=metadata or {},
        )

    def _announce():
        logger.info(
            "automation.instance.transition",
            extra={
                "automation_instance_id": instance.pk,
                "from_status": from_status,
                "to_status": to_status,
            },
        )
        if to_status in TERMINAL_STATES:
            try:
                instance_finished.send(sender=AutomationInstance, instance=instance, status=to_status)
            except Exception:
                logger.exception("automation.signal.failed", extra={"automation_instance_id": instance.pk})

    # Same reasoning as for actions: announce only what has actually landed.
    transaction.on_commit(_announce)
    return instance

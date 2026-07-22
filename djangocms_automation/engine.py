"""Execution engine for automation workflows.

This module owns the orchestration of automation runs:

* claiming actions (idempotent, race-free state transitions),
* building the plugin tree data structure,
* dispatching ``plugin.execute()`` and handling its outcome,
* scheduling follow-up actions, waking waiting parents (joins),
* failure propagation and instance completion,
* pausing and reviving actions.

Module responsibilities: :mod:`.instances` holds the runtime state models,
:mod:`.models` holds the graph-node (plugin) models with their node-local
``execute``/``get_next_actions`` behavior, and :mod:`.tasks` holds only the
``django.tasks`` entry points delegating here.

The execute contract for plugins is::

    def execute(self, action, data, single_step=False, plugin_dict=None):
        return state, output

where ``data`` is the normalized list of data rows flowing through the
automation, ``state`` is one of the :mod:`.instances` state constants and
``output`` is the data passed to subsequent actions (canonically a list of
dict rows). Raising :class:`ActionPause` pauses the action until a given
time; any other exception fails the action and the automation instance.
"""

from __future__ import annotations

import datetime
import traceback
import uuid as uuid_module

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.utils.timezone import now

from cms.models import CMSPlugin, Placeholder
from cms.utils.plugins import downcast_plugins, get_plugins_as_layered_tree

from .instances import (
    COMPLETED,
    FAILED,
    MAX_FIELD_LENGTH,
    PENDING,
    RUNNING,
    WAITING,
    AutomationAction,
    AutomationInstance,
)
from .transitions import transition_action

__all__ = [
    "ActionPause",
    "build_plugin_map",
    "claim_action",
    "run_action",
    "normalize_rows",
    "enqueue_action",
    "notify_parent",
    "fail_action",
    "propagate_failure",
    "maybe_finish_instance",
    "pause_action",
    "resume_action",
    "revive_pending",
]


class ActionPause(Exception):
    """Raised by an action's ``perform``/``execute`` to pause the action.

    The engine sets the action back to ``PENDING`` with ``paused_until``;
    the ``runautomations`` management command revives it once due.
    """

    def __init__(self, until: datetime.datetime, message: str = ""):
        self.until = until
        self.message = message
        super().__init__(message or f"Paused until {until}")


def normalize_rows(data) -> list[dict]:
    """Normalize automation data to the canonical list-of-rows shape.

    ``None`` becomes ``[]``, a dict becomes a single-row list, and a list is
    passed through. Any other value is wrapped in a ``{"value": ...}`` row.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data] if data else []
    return [{"value": data}]


def _link_tree(plugins: list[CMSPlugin]) -> None:
    """Recursively link sibling plugins into a doubly-linked list.

    Sets ``previous_plugin_instance`` / ``next_plugin_instance`` on each
    plugin and recurses into child plugins.
    """
    previous_plugin = None
    for plugin in plugins:
        _link_tree(plugin.child_plugin_instances)
        if previous_plugin:
            previous_plugin.next_plugin_instance = plugin
        plugin.previous_plugin_instance = previous_plugin
        previous_plugin = plugin
    if previous_plugin is not None:
        previous_plugin.next_plugin_instance = None


def build_plugin_map(automation_content_id: int) -> dict[uuid_module.UUID, CMSPlugin]:
    """Build the linked, downcast plugin tree for an automation content.

    :returns: Mapping of plugin ``uuid`` to the downcast plugin instance.
        Plugins without a ``uuid`` attribute (non-automation plugins) are
        omitted from the map but still linked in the tree.
    """
    from .models import AutomationContent

    placeholders = Placeholder.objects.filter(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content_id,
    )
    plugins = list(CMSPlugin.objects.filter(placeholder__in=placeholders, language=settings.LANGUAGE_CODE))
    plugins = list(downcast_plugins(plugins, placeholders, select_placeholder=True))
    root_plugins = get_plugins_as_layered_tree(plugins)
    _link_tree(root_plugins)
    return {plugin.uuid: plugin for plugin in plugins if hasattr(plugin, "uuid")}


def _is_immediate_backend() -> bool:
    """Check whether the configured task backend is the ImmediateBackend.

    The ``ImmediateBackend`` runs tasks synchronously in-process inside the
    current transaction, so ``transaction.on_commit`` callbacks never fire.
    We detect it via ``settings.TASKS`` rather than inspecting the
    ``ConnectionProxy`` that wraps the actual backend at runtime.
    """
    from django.conf import settings

    backend = settings.TASKS.get("default", {}).get("BACKEND", "")
    return backend.endswith(".ImmediateBackend")


def _fail_enqueue(action_id: int, exc: BaseException) -> None:
    """Mark an action as FAILED because its task could not be enqueued.

    Called when the task backend rejects an enqueue (broker unavailable,
    serialization failure, etc.). The error is recorded in the action's
    ``result`` field and failure propagates to ancestors and the instance.
    """
    from .instances import AutomationAction

    action = AutomationAction.objects.filter(pk=action_id).first()
    if action is None or action.finished is not None:
        return  # Already gone — nothing to fail.
    action = transition_action(
        action_id,
        FAILED,
        result={"error": "Task enqueue failed", "detail": str(exc)},
        message=f"Task enqueue failed: {exc}"[:MAX_FIELD_LENGTH],
        error=exc,
        unfinished_only=True,
    )
    if action is None:
        return
    propagate_failure(action)


def _safe_enqueue(enqueue_fn, action_id: int) -> None:
    """Call the enqueue function, failing the action on error.

    Used as a ``transaction.on_commit`` callback so that enqueue failures
    inside deferred callbacks are still recorded rather than lost.
    """
    try:
        enqueue_fn()
    except Exception as exc:  # noqa: BLE001 — must not leak into the commit hook
        _fail_enqueue(action_id, exc)


def enqueue_action(action_id: int, data=None, single_step: bool = False) -> None:
    """Enqueue an action for execution via the task backend.

    The enqueue is deferred until the current database transaction commits
    to avoid running tasks against uncommitted (or rolled-back) state.
    The ``ImmediateBackend`` (used in tests) is detected at runtime and
    bypasses the deferral since it runs synchronously in-process, where
    on-commit callbacks would never fire.

    If the task backend rejects the enqueue (e.g. broker unavailable), the
    action is marked FAILED with the rejection reason stored in its result.
    """
    from .tasks import execute_action

    def _do_enqueue():
        execute_action.enqueue(action_id, data=data, single_step=single_step)

    if _is_immediate_backend():
        try:
            _do_enqueue()
        except Exception as exc:
            _fail_enqueue(action_id, exc)
    else:
        transaction.on_commit(lambda: _safe_enqueue(_do_enqueue, action_id))


def claim_action(action_id: int, allow_states: tuple[str, ...] = (PENDING,)) -> AutomationAction | None:
    """Atomically claim an action for execution (``PENDING`` → ``RUNNING``).

    :returns: The claimed action, or ``None`` if it was already claimed,
        finished, or in a non-claimable state (making double enqueues no-ops).
    """
    action = transition_action(
        action_id,
        RUNNING,
        allowed_from=allow_states,
        unfinished_only=True,
    )
    if action is None:
        return None
    return AutomationAction.objects.select_related(
        "automation_instance", "automation_instance__automation_content"
    ).get(pk=action_id)


def fail_action(action: AutomationAction, message: str, *, exc: BaseException | None = None) -> None:
    """Mark an action as failed and propagate the failure."""
    result = {"error": message}
    if exc is not None:
        result["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    failed = transition_action(
        action.pk,
        FAILED,
        result=result,
        message=message[:MAX_FIELD_LENGTH],
        error=exc,
    )
    if failed is not None:
        propagate_failure(failed)


def propagate_failure(action: AutomationAction) -> None:
    """Fail-fast propagation: fail unfinished ancestors and the instance."""
    timestamp = now()
    parent_id = action.parent_id
    failed_id = action.pk
    while parent_id:
        parent = AutomationAction.objects.filter(pk=parent_id).first()
        if parent is None or parent.finished is not None:
            break
        AutomationAction.objects.filter(pk=parent.pk, finished__isnull=True).update(
            state=FAILED,
            finished=timestamp,
            message="Branch failed",
            result={"failed_action_id": failed_id},
        )
        failed_id = parent.pk
        parent_id = parent.parent_id
    AutomationInstance.objects.filter(pk=action.automation_instance_id, finished__isnull=True).update(
        status=FAILED, finished=timestamp
    )


def maybe_finish_instance(instance: AutomationInstance) -> None:
    """Mark the instance completed once no unfinished actions remain."""
    has_open = AutomationAction.objects.filter(automation_instance=instance, finished__isnull=True).exists()
    if not has_open:
        AutomationInstance.objects.filter(pk=instance.pk, finished__isnull=True).update(
            status=COMPLETED, finished=now()
        )


def notify_parent(action: AutomationAction, data=None) -> bool:
    """Wake a waiting parent action (join point) exactly once.

    Atomically flips the parent ``WAITING`` → ``PENDING``; only the child
    that wins the flip enqueues the parent, so concurrent completions of
    sibling branches cannot double-schedule it.

    :returns: True if the parent was woken by this call.
    """
    woken = AutomationAction.objects.filter(pk=action.parent_id, state=WAITING).update(state=PENDING)
    if woken:
        enqueue_action(action.parent_id, data=data)
    return bool(woken)


def _wake_if_children_done(action: AutomationAction) -> None:
    """Close the lost-wakeup window for actions that just went ``WAITING``.

    If all children finished while the parent was still ``RUNNING`` (their
    ``notify_parent`` found no ``WAITING`` row), re-enqueue the parent now.
    """
    children = AutomationAction.objects.filter(parent=action)
    if children.exists() and not children.filter(finished__isnull=True).exists():
        if AutomationAction.objects.filter(pk=action.pk, state=WAITING).update(state=PENDING):
            enqueue_action(action.pk)


def pause_action(action: AutomationAction, until: datetime.datetime, message: str = "") -> None:
    """Pause an action until a given time (revived by ``revive_pending``)."""
    action = transition_action(
        action.pk,
        PENDING,
        allowed_from=(PENDING, RUNNING),
        result=action.result,
        message=message[:MAX_FIELD_LENGTH] if message else None,
    )
    if action is None:
        return
    action.paused_until = until
    action.save(update_fields=["paused_until"])


def run_action(action_id: int, data=None, single_step: bool = False) -> None:
    """Execute a single automation action and schedule what follows."""
    action = claim_action(action_id)
    if action is None:
        return

    plugin_map = build_plugin_map(action.automation_instance.automation_content_id)
    plugin = plugin_map.get(action.plugin_ptr)
    if plugin is None:
        fail_action(action, "Plugin no longer exists in the automation")
        return
    action._plugin = plugin

    rows = normalize_rows(data if data is not None else action.automation_instance.data)

    try:
        state, output = plugin.execute(action, rows, single_step=single_step, plugin_dict=plugin_map)
    except ActionPause as pause:
        pause_action(action, until=pause.until, message=pause.message)
        return
    except Exception as exc:  # noqa: BLE001 - any action error fails the run
        fail_action(action, str(exc), exc=exc)
        return

    if state == FAILED:
        message = output.get("error", "Action failed") if isinstance(output, dict) else "Action failed"
        fail_action(action, message)
        return

    transition_kwargs = {
        "allowed_from": (RUNNING,),
        "message": action.message if action.message else None,
        "field_updates": {
            "requires_interaction": action.requires_interaction,
            "interaction_permissions": action.interaction_permissions,
            "interaction_user_id": action.interaction_user_id,
            "interaction_group_id": action.interaction_group_id,
        },
    }
    # Preserve existing branch/retry metadata when a plugin has no output.
    # This matches the engine's behavior before transitions were introduced.
    if output:
        transition_kwargs["result"] = output
    transitioned = transition_action(action.pk, state, **transition_kwargs)
    if transitioned is None:
        return
    action = transitioned

    if single_step:
        return

    next_actions = plugin.get_next_actions(action)
    if next_actions:
        # Fan-out states pass the incoming rows through; completed actions
        # hand their output to the next action(s).
        payload = output if state == COMPLETED else rows
        for next_action in next_actions:
            enqueue_action(next_action.pk, data=payload)
        return

    if state == COMPLETED:
        if action.parent_id:
            notify_parent(action)
        else:
            instance = action.automation_instance
            instance.data = output
            instance.save()
            maybe_finish_instance(instance)
    elif state == WAITING:
        _wake_if_children_done(action)


def resume_action(action_id: int, user, data: dict | None = None) -> AutomationAction:
    """Resume a ``WAITING`` action that requires user interaction.

    :param user: The user resuming the action; must be permitted via
        :meth:`AutomationAction.get_users_with_permission`.
    :param data: Optional extra data merged into the automation data as an
        additional row.
    :raises PermissionError: If the user may not interact with this action.
    :raises ValueError: If the action is not waiting for interaction.
    """
    action = AutomationAction.objects.select_related("automation_instance").get(pk=action_id)
    if not action.requires_interaction or action.finished is not None:
        raise ValueError("Action is not awaiting user interaction.")
    if user not in action.get_users_with_permission():
        raise PermissionError("User may not interact with this action.")

    rows = normalize_rows(action.automation_instance.data)
    if data:
        rows = rows + [data]

    claimed = AutomationAction.objects.filter(pk=action.pk, state=WAITING).update(
        state=COMPLETED, finished=now(), requires_interaction=False
    )
    if not claimed:
        raise ValueError("Action is no longer waiting.")
    action.refresh_from_db()

    plugin_map = build_plugin_map(action.automation_instance.automation_content_id)
    plugin = plugin_map.get(action.plugin_ptr)
    if plugin is None:
        fail_action(action, "Plugin no longer exists in the automation")
        return action

    next_actions = plugin.get_next_actions(action)
    if next_actions:
        for next_action in next_actions:
            enqueue_action(next_action.pk, data=rows)
    elif action.parent_id:
        notify_parent(action)
    else:
        instance = action.automation_instance
        instance.data = rows
        instance.save()
        maybe_finish_instance(instance)
    return action


def revive_pending(timestamp: datetime.datetime | None = None) -> int:
    """Enqueue all due ``PENDING`` actions (paused or stalled).

    :returns: The number of actions enqueued.
    """
    timestamp = timestamp or now()
    actions = AutomationAction.objects.filter(
        Q(paused_until=None) | Q(paused_until__lte=timestamp),
        finished__isnull=True,
        state=PENDING,
        automation_instance__automation_content__automation__is_active=True,
    )
    count = 0
    for action in actions:
        enqueue_action(action.pk)
        count += 1
    return count


def _add_months(value: datetime.datetime, months: int) -> datetime.datetime:
    """Add calendar months to a datetime, clamping the day of month."""
    import calendar

    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return value.replace(year=year, month=month, day=min(value.day, last_day))


_FREQUENCY_STEPS = {
    "hourly": datetime.timedelta(hours=1),
    "daily": datetime.timedelta(days=1),
    "weekly": datetime.timedelta(weeks=1),
}


def _parse_datetime(value) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _next_timer_fire(config: dict, timestamp: datetime.datetime) -> datetime.datetime | None:
    """Compute the next due fire time for a timer trigger config.

    :returns: The next occurrence at or before ``timestamp``, or ``None``
        if the timer is not due (or exhausted).
    """
    scheduled_at = _parse_datetime(config.get("scheduled_at"))
    if scheduled_at is None:
        return None
    last_fired = _parse_datetime(config.get("last_fired"))
    if last_fired is None:
        return scheduled_at if scheduled_at <= timestamp else None

    frequency = config.get("recurrence_frequency") or ""
    if not frequency:
        return None  # One-shot timer already fired.
    interval = int(config.get("recurrence_interval") or 1)
    count_limit = config.get("recurrence_count")
    if count_limit and int(config.get("fired_count", 1)) >= int(count_limit):
        return None
    if frequency == "monthly":
        next_fire = _add_months(last_fired, interval)
    else:
        step = _FREQUENCY_STEPS.get(frequency)
        if step is None:
            return None
        next_fire = last_fired + interval * step
    end_date = _parse_datetime(config.get("recurrence_end_date"))
    if end_date and next_fire > end_date:
        return None
    return next_fire if next_fire <= timestamp else None


def fire_due_timers(timestamp: datetime.datetime | None = None) -> int:
    """Fire all due timer triggers on active automations.

    A one-shot timer fires once when ``scheduled_at`` is reached; recurring
    timers step forward by their configured frequency/interval (simple
    wall-clock stepping — DST-exact recurrence is out of scope). The last
    fire time and count are stamped back into the trigger config.

    :returns: The number of triggers fired.
    """
    from .models import AutomationTrigger

    timestamp = timestamp or now()
    fired = 0
    triggers = AutomationTrigger.objects.filter(
        type="timer", automation_content__automation__is_active=True
    ).select_related("automation_content")
    for trigger in triggers:
        config = dict(trigger.config or {})
        due = _next_timer_fire(config, timestamp)
        if due is None:
            continue
        trigger.trigger_execution(data=[{"scheduled_at": due.isoformat(), "fired_at": timestamp.isoformat()}])
        config["last_fired"] = due.isoformat()
        config["fired_count"] = int(config.get("fired_count", 0)) + 1
        trigger.config = config
        trigger.save(update_fields=["config"])
        fired += 1
    return fired

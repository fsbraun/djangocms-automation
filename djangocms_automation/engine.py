"""Execution engine for automation workflows.

This module owns the orchestration of automation runs:

* claiming actions (idempotent, race-free state transitions),
* building the plugin tree data structure,
* dispatching ``plugin.execute()`` and handling its outcome,
* scheduling follow-up actions, waking waiting parents (joins),
* failure propagation and instance completion,
* pausing and reviving actions.

Module responsibilities: :mod:`.instances` holds the runtime state models,
:mod:`djangocms_automation.models` holds the graph-node (plugin) models with their node-local
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
import logging
import threading
import traceback
import uuid as uuid_module

from cms.models import CMSPlugin, Placeholder
from cms.utils.plugins import downcast_plugins, get_plugins_as_layered_tree
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction
from django.db.models import Q
from django.utils.timezone import now

from .instances import (
    CANCELED,
    COMPLETED,
    FAILED,
    MAX_FIELD_LENGTH,
    PENDING,
    RUNNING,
    WAITING,
    AutomationAction,
    AutomationInstance,
    SchedulerLock,
)
from .retry import DEFAULT_RETRY_POLICY
from .signals import action_dead_lettered
from .transitions import transition_action, transition_instance

logger = logging.getLogger("djangocms_automation.engine")

__all__ = [
    "ActionPause",
    "build_plugin_map",
    "cancel_instance",
    "claim_action",
    "enqueue_action",
    "fail_action",
    "maybe_finish_instance",
    "normalize_rows",
    "notify_parent",
    "pause_action",
    "propagate_failure",
    "reconcile_stalled_instances",
    "reconcile_waiting_joins",
    "recover_expired_leases",
    "replay_action",
    "resume_action",
    "revive_pending",
    "run_action",
    "scheduler_lock",
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
    with transaction.atomic():
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
        dead_letter(action)
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
        except Exception as exc:  # noqa: BLE001 — any enqueue failure must be recorded, not raised
            _fail_enqueue(action_id, exc)
    else:
        transaction.on_commit(lambda: _safe_enqueue(_do_enqueue, action_id))


def claim_action(
    action_id: int,
    allow_states: tuple[str, ...] = (PENDING,),
    field_updates: dict | None = None,
) -> AutomationAction | None:
    """Atomically claim an action for execution (``PENDING`` → ``RUNNING``).

    :param field_updates: Written in the same transaction as the claim. The
        execution policy — timeout, retry budget, the input this attempt was
        given — must land with the claim rather than after it: a worker that
        dies in between would otherwise leave recovery with no stored budget,
        and the action would be dead-lettered on its first expired lease even
        though its plugin allowed retries.
    :returns: The claimed action, or ``None`` if it was already claimed,
        finished, or in a non-claimable state (making double enqueues no-ops).
    """
    action = transition_action(
        action_id,
        RUNNING,
        allowed_from=allow_states,
        unfinished_only=True,
        field_updates=field_updates,
    )
    if action is None:
        return None
    return AutomationAction.objects.select_related(
        "automation_instance", "automation_instance__automation_content"
    ).get(pk=action_id)


def get_retry_policy(action: AutomationAction):
    """Resolve the retry policy governing an action.

    The policy comes from the plugin model; a plugin that declares none gets
    :data:`~djangocms_automation.retry.DEFAULT_RETRY_POLICY`, which does not
    retry. This keeps historical fail-fast behavior for every action that has
    not opted in.
    """
    plugin = getattr(action, "_plugin", None)
    return getattr(plugin, "retry_policy", DEFAULT_RETRY_POLICY)


def effective_max_attempts(action: AutomationAction, policy) -> int:
    """Combine the plugin's policy with any per-action override.

    The row's ``max_attempts`` defaults to 1, so the policy normally wins.
    Raising the row value above the policy is the documented per-action
    override and takes precedence.
    """
    return max(action.max_attempts or 1, policy.max_attempts)


def schedule_retry(
    action: AutomationAction, exc: BaseException, policy, require_lease=None
) -> AutomationAction | None:
    """Reschedule a failed action for a later attempt.

    The action returns to ``PENDING`` with ``next_attempt_at`` set; the
    scheduler enqueues it once due. No new logical action is created — the
    attempt history accumulates on the same row.
    """
    delay = policy.next_delay(action.attempt_count, exc)
    due = now() + datetime.timedelta(seconds=delay)
    retried = transition_action(
        action.pk,
        PENDING,
        allowed_from=(RUNNING,),
        message=f"Retry {action.attempt_count}/{effective_max_attempts(action, policy)} after {delay:.0f}s"[
            :MAX_FIELD_LENGTH
        ],
        error=exc,
        metadata={"retry_in_seconds": round(delay, 3)},
        field_updates={"next_attempt_at": due, "paused_until": due},
        require_lease=require_lease,
    )
    if retried is not None:
        logger.info(
            "automation.action.retry_scheduled",
            extra={
                "automation_action_id": action.pk,
                "attempt": action.attempt_count,
                "retry_in_seconds": round(delay, 3),
            },
        )
    return retried


def dead_letter(action: AutomationAction) -> None:
    """Mark a terminally failed action as awaiting inspection or replay."""
    AutomationAction.objects.filter(pk=action.pk, dead_lettered=False).update(
        dead_lettered=True, dead_lettered_at=now()
    )
    action.dead_lettered = True
    logger.warning(
        "automation.action.dead_lettered",
        extra={
            "automation_action_id": action.pk,
            "automation_instance_id": action.automation_instance_id,
            "attempt": action.attempt_count,
        },
    )

    def _announce():
        try:
            action_dead_lettered.send(sender=AutomationAction, action=action)
        except Exception:
            logger.exception("automation.signal.failed", extra={"automation_action_id": action.pk})

    # Dead-lettering now happens inside the transaction that fails the action,
    # so the announcement waits for that to land.
    transaction.on_commit(_announce)


def fail_action(
    action: AutomationAction,
    message: str,
    *,
    exc: BaseException | None = None,
    require_lease=None,
    allowed_from: tuple[str, ...] | None = None,
) -> None:
    """Fail an action, retrying first if its policy allows.

    A retryable failure with attempts remaining reschedules instead of failing,
    and does not propagate. Anything else fails the action, adds it to the
    dead-letter queue so it can be inspected and replayed, and fails its
    ancestors and the instance.
    """
    policy = get_retry_policy(action)
    if exc is not None and action.state == RUNNING:
        max_attempts = effective_max_attempts(action, policy)
        if policy.should_retry(exc, action.attempt_count, max_attempts) and schedule_retry(
            action, exc, policy, require_lease=require_lease
        ):
            return

    result = {"error": message}
    if exc is not None:
        result["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # As above: a failure that commits without its consequences leaves an action
    # marked FAILED while the instance still looks alive and nothing appears in
    # the dead-letter queue. The three belong in one commit.
    with transaction.atomic():
        failed = transition_action(
            action.pk,
            FAILED,
            allowed_from=allowed_from,
            result=result,
            message=message[:MAX_FIELD_LENGTH],
            error=exc,
            require_lease=require_lease,
        )
        if failed is None:
            return
        failed._plugin = getattr(action, "_plugin", None)
        dead_letter(failed)
        propagate_failure(failed)


def propagate_failure(action: AutomationAction) -> None:
    """Fail-fast propagation: fail unfinished ancestors and the instance.

    Ancestors are failed through the transition service so the propagation is
    recorded in the event history like any other transition, rather than
    disappearing into a bulk update.
    """
    parent_id = action.parent_id
    failed_id = action.pk
    while parent_id:
        parent = AutomationAction.objects.filter(pk=parent_id).first()
        if parent is None or parent.finished is not None:
            break
        transitioned = transition_action(
            parent.pk,
            FAILED,
            result={"failed_action_id": failed_id},
            message="Branch failed",
            unfinished_only=True,
            metadata={"propagated_from": failed_id},
        )
        if transitioned is None:
            break
        failed_id = parent.pk
        parent_id = parent.parent_id
    finish_instance(action.automation_instance_id, FAILED)


def finish_instance(instance_id: int, status: str) -> bool:
    """Move an instance to a terminal status exactly once.

    :returns: True if this call finished the instance.
    """
    instance = transition_instance(instance_id, status, unfinished_only=True)
    if instance is None:
        return False
    notify_parent_action(instance, status)
    return True


def notify_parent_action(instance: AutomationInstance, status: str) -> bool:
    """Wake the action that started this run, if one did.

    A run started from inside another automation — an agent's tool call, a
    sub-workflow step — leaves an action waiting on something no other wake-up
    reaches. It is not waiting for a person, and the run is not its child
    action, so neither the human-in-the-loop resume nor the join applies.

    Done here rather than from the ``instance_finished`` signal, which is
    deliberately best-effort: receivers are isolated so an exception in one
    cannot disturb the transition it observes. That is right for an observer
    and wrong for this, because a wake-up that is quietly dropped leaves the
    caller waiting for something that already happened.

    :returns: True if a waiting caller was woken by this call.
    """
    if instance.parent_action_id is None:
        return False
    caller = AutomationAction.objects.filter(pk=instance.parent_action_id, state=WAITING).first()
    if caller is None:
        return False
    scratch = dict(caller.scratch or {})
    scratch["called"] = {
        "instance": instance.pk,
        "status": status,
        "data": instance.data if status == COMPLETED else None,
    }
    woken = transition_action(
        caller.pk,
        PENDING,
        allowed_from=(WAITING,),
        continuation=True,
        message="Called automation finished",
        field_updates={"scratch": scratch},
        metadata={"woken_by_instance": instance.pk},
    )
    if woken is None:
        return False
    enqueue_action(caller.pk, data=instance.data or [])
    return True


def maybe_finish_instance(instance: AutomationInstance) -> None:
    """Mark the instance completed once no unfinished actions remain."""
    has_open = AutomationAction.objects.filter(automation_instance=instance, finished__isnull=True).exists()
    if not has_open:
        finish_instance(instance.pk, COMPLETED)


def notify_parent(action: AutomationAction, data=None) -> bool:
    """Wake a waiting parent action (join point) exactly once.

    Atomically flips the parent ``WAITING`` → ``PENDING``; only the child
    that wins the flip enqueues the parent, so concurrent completions of
    sibling branches cannot double-schedule it.

    :returns: True if the parent was woken by this call.
    """
    woken = transition_action(
        action.parent_id,
        PENDING,
        allowed_from=(WAITING,),
        continuation=True,
        metadata={"woken_by": action.pk},
    )
    if woken is not None:
        enqueue_action(action.parent_id, data=data)
    return woken is not None


def _wake_if_children_done(action: AutomationAction) -> None:
    """Close the lost-wakeup window for actions that just went ``WAITING``.

    If all children finished while the parent was still ``RUNNING`` (their
    ``notify_parent`` found no ``WAITING`` row), re-enqueue the parent now.

    The same window exists one level up. An action that started another
    automation is not waiting for child actions but for a whole run, and that
    run can finish before this action is marked as waiting for it — trivially
    so with an immediate task backend, where the run happens inside the call
    that starts it. Its ``notify_parent_action`` then finds nothing waiting.
    """
    finished_run = (
        AutomationInstance.objects.filter(parent_action=action, finished__isnull=False).order_by("-finished").first()
    )
    if finished_run is not None:
        notify_parent_action(finished_run, finished_run.status)
        return

    children = AutomationAction.objects.filter(parent=action)
    if children.exists() and not children.filter(finished__isnull=True).exists():
        woken = transition_action(
            action.pk,
            PENDING,
            allowed_from=(WAITING,),
            continuation=True,
            metadata={"woken_by": "lost_wakeup_guard"},
        )
        if woken is not None:
            enqueue_action(action.pk)


def pause_action(action: AutomationAction, until: datetime.datetime, message: str = "", require_lease=None) -> None:
    """Pause an action until a given time (revived by ``revive_pending``).

    A pause is a deliberate reschedule, not a failure, so it is marked as a
    continuation: the next claim counts as a re-entry and leaves the action's
    retry budget intact.
    """
    action = transition_action(
        action.pk,
        PENDING,
        allowed_from=(PENDING, RUNNING),
        result=action.result,
        message=message[:MAX_FIELD_LENGTH] if message else None,
        continuation=True,
        field_updates={"paused_until": until},
        require_lease=require_lease,
    )


class _Heartbeat:
    """Refresh a running action's lease from a background thread.

    The engine claims an action, stamps ``heartbeat_at``, and then hands control
    to ``plugin.execute()``. On a durable backend that commits the claim, an
    action legitimately running longer than ``AUTOMATION_LEASE_SECONDS`` would
    otherwise look abandoned to the scheduler, which would recover it and run it
    a second time — precisely the duplicate side effect leases exist to prevent.

    A daemon thread refreshes the lease for as long as the action runs.
    ``heartbeat_action`` only updates rows that still hold the lease, so a stale
    thread can never revive an action someone else has taken over.

    **It never gives up on its own.** A database outage is exactly when renewal
    matters most: the action keeps executing regardless, so a thread that
    stopped retrying would leave a stale heartbeat behind, and the scheduler
    would start a duplicate as soon as the database came back. Failures are
    retried indefinitely with capped backoff, and the thread's connection is
    dropped after each one so a broken socket is replaced rather than reused.

    There are exactly two ways out: the action finishes (``__exit__`` sets the
    stop event) or the lease is lost, which means someone else owns the action
    and this thread must stop touching it.
    """

    def __init__(self, action_id: int, lease_id, interval: float):
        self.action_id = action_id
        self.lease_id = lease_id
        self.interval = max(1.0, interval)
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if self.lease_id is None:
            return self
        self._thread = threading.Thread(target=self._run, name=f"automation-heartbeat-{self.action_id}", daemon=True)
        self._thread.start()
        return self

    #: How often a continuing outage is logged, in failures. Retrying forever
    #: must not also mean logging forever.
    log_every = 10

    def _first_retry_delay(self) -> float:
        """Retry sooner than the normal interval: the lease is already at risk."""
        return max(0.05, self.interval / 8)

    def _recycle_connection(self) -> None:
        """Drop this thread's database connection so the next try gets a fresh one.

        A failed refresh usually means the connection is broken, not that the
        database is gone. Without this the thread would keep reusing the same
        dead socket and never recover.
        """
        try:
            from django.db import connection

            connection.close()
        except Exception:
            logger.debug("automation.heartbeat.connection_recycle_failed", exc_info=True)

    def _run(self):
        from .transitions import heartbeat_action

        failures = 0
        delay = self.interval
        retry_delay = self._first_retry_delay()
        try:
            while not self._stop.wait(delay):
                try:
                    if not heartbeat_action(self.action_id, self.lease_id):
                        return  # Lease lost or action finished; stop quietly.
                except Exception:
                    failures += 1
                    if failures == 1 or failures % self.log_every == 0:
                        logger.warning(
                            "automation.heartbeat.failed",
                            extra={"automation_action_id": self.action_id, "consecutive_failures": failures},
                            exc_info=True,
                        )
                    self._recycle_connection()
                    delay = retry_delay
                    # Back off, but never past the normal interval: a recovered
                    # database must still be noticed inside the lease window.
                    retry_delay = min(self.interval, retry_delay * 2)
                    continue
                if failures:
                    logger.info(
                        "automation.heartbeat.recovered",
                        extra={"automation_action_id": self.action_id, "consecutive_failures": failures},
                    )
                failures = 0
                delay = self.interval
                retry_delay = self._first_retry_delay()
        finally:
            # Django opens a connection per thread; this one is ours to close.
            self._recycle_connection()

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False


def _heartbeat_interval() -> float:
    """Refresh well inside the lease window, so one missed beat is not fatal."""
    window = float(getattr(settings, "AUTOMATION_LEASE_SECONDS", 300))
    return max(1.0, window / 3)


def _resolve_timeout(plugin) -> int | None:
    """Resolve the execution timeout for a plugin, in seconds."""
    timeout = getattr(plugin, "timeout_seconds", None)
    if timeout is None:
        timeout = getattr(settings, "AUTOMATION_ACTION_TIMEOUT", None)
    return int(timeout) if timeout else None


def run_action(action_id: int, data=None, single_step: bool = False) -> None:
    """Execute a single automation action and schedule what follows."""
    # Resolve the plugin *before* claiming, so the execution policy it implies
    # can be written in the same transaction as the claim. Claiming first would
    # leave a window in which a dying worker strands an action with no recorded
    # timeout or retry budget for recovery to work from.
    pending = (
        AutomationAction.objects.select_related("automation_instance", "automation_instance__automation_content")
        .filter(pk=action_id)
        .first()
    )
    if pending is None:
        return

    plugin_map = build_plugin_map(pending.automation_instance.automation_content_id)
    plugin = plugin_map.get(pending.plugin_ptr)
    rows = normalize_rows(data if data is not None else pending.automation_instance.data)

    claim_updates = None
    if plugin is not None:
        pending._plugin = plugin
        claim_updates = {
            # The input this attempt was given, so a dead-lettered action can be
            # replayed with it rather than whatever the instance holds later.
            "input_data": rows,
            "timeout_seconds": _resolve_timeout(plugin),
            # Lease recovery runs in the scheduler, which has no plugin instance
            # to ask, so the budget has to be on the row.
            "max_attempts": effective_max_attempts(pending, get_retry_policy(pending)),
        }

    action = claim_action(action_id, field_updates=claim_updates)
    if action is None:
        return

    # Everything this worker writes from here on is fenced to the lease it just
    # took. If the lease expires and another worker claims the action, this
    # worker's late writes are discarded instead of landing on the new attempt.
    lease = action.lease_id

    if action.automation_instance.status == CANCELED:
        # The run was canceled between enqueue and claim: stop without side effects.
        transition_action(
            action.pk, CANCELED, allowed_from=(RUNNING,), message="Instance canceled", require_lease=lease
        )
        return

    if plugin is None:
        fail_action(action, "Plugin no longer exists in the automation", require_lease=lease)
        return
    action._plugin = plugin

    try:
        with _Heartbeat(action.pk, action.lease_id, _heartbeat_interval()):
            state, output = plugin.execute(action, rows, single_step=single_step, plugin_dict=plugin_map)
    except ActionPause as pause:
        pause_action(action, until=pause.until, message=pause.message, require_lease=lease)
        return
    except Exception as exc:  # noqa: BLE001 - any action error fails the run
        fail_action(action, str(exc), exc=exc, require_lease=lease)
        return

    if state == FAILED:
        message = output.get("error", "Action failed") if isinstance(output, dict) else "Action failed"
        fail_action(action, message, require_lease=lease)
        return

    transition_kwargs = {
        "allowed_from": (RUNNING,),
        "require_lease": lease,
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
    # Recording the outcome and scheduling what follows must be one unit. Split
    # across two commits, a worker dying in between leaves an action that is
    # finished with nothing queued behind it: the run stalls with no unfinished
    # action for recovery to find and no successor to make progress. Committed
    # together, a crash rolls both back, the action stays ``RUNNING``, and lease
    # recovery reclaims it like any other abandoned attempt.
    #
    # ``enqueue_action`` already defers the dispatch itself to ``on_commit``, so
    # nothing reaches a worker until the outcome is durable.
    with transaction.atomic():
        transitioned = transition_action(action.pk, state, **transition_kwargs)
        if transitioned is None:
            return
        action = transitioned

        if single_step:
            return

        next_actions = plugin.get_next_actions(action)
        if next_actions:
            payload = plugin.get_next_payload(action, state, output, rows)
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

    # Reject a stale claim before doing anything else. The transition below is
    # still the authority under lock; this only keeps an action that is no
    # longer waiting from being failed by the missing-plugin path.
    if action.state != WAITING:
        raise ValueError("Action is no longer waiting.")

    plugin_map = build_plugin_map(action.automation_instance.automation_content_id)
    plugin = plugin_map.get(action.plugin_ptr)
    if plugin is None:
        # Fail from WAITING, before the resume. Completing first and failing
        # afterwards asks for COMPLETED -> FAILED, which the lifecycle forbids:
        # the whole transaction would roll back and leave the action waiting,
        # permanently unresumable.
        #
        # ``allowed_from`` makes the source check atomic. The WAITING test above
        # was made against an object loaded earlier; two concurrent resumes
        # would both pass it, and the second would ask for FAILED -> FAILED and
        # raise. Guarded, the loser simply finds nothing to do.
        fail_action(action, "Plugin no longer exists in the automation", allowed_from=(WAITING,))
        action.refresh_from_db()
        return action

    # Some nodes pause *before* doing their work, so resuming them means "go
    # ahead" rather than "you are done". Those re-enter: the decision is
    # recorded, the action goes back to PENDING as a continuation — not a
    # retry, it has not failed at anything — and it runs again.
    if plugin.resume_reenters(action):
        with transaction.atomic():
            plugin.on_resume(action, user, data)
            woken = transition_action(
                action.pk,
                PENDING,
                allowed_from=(WAITING,),
                message="Resumed by user",
                field_updates={"requires_interaction": False},
                metadata={"resumed_by": getattr(user, "pk", None)},
                continuation=True,
            )
            if woken is None:
                raise ValueError("Action is no longer waiting.")
            enqueue_action(action.pk, data=rows)
        return woken

    # The resume and what follows it commit together, for the same reason as an
    # action's outcome: a resume that lands without its continuation leaves a
    # completed task and a run with nothing queued behind it.
    with transaction.atomic():
        claimed = transition_action(
            action.pk,
            COMPLETED,
            allowed_from=(WAITING,),
            message="Resumed by user",
            field_updates={"requires_interaction": False},
            metadata={"resumed_by": getattr(user, "pk", None)},
        )
        if claimed is None:
            raise ValueError("Action is no longer waiting.")
        action = claimed

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
        Q(next_attempt_at=None) | Q(next_attempt_at__lte=timestamp),
        finished__isnull=True,
        state=PENDING,
        automation_instance__automation_content__automation__is_active=True,
    ).exclude(automation_instance__status=CANCELED)
    # Hold each revived action off for a lease window. Without it every tick
    # re-enqueues the same due actions, so a worker outage builds one duplicate
    # queue row per action per tick. Claiming clears ``next_attempt_at``, so an
    # action a worker actually picks up is unaffected; one that is not picked up
    # simply comes round again on a later tick.
    hold_until = timestamp + datetime.timedelta(seconds=float(getattr(settings, "AUTOMATION_LEASE_SECONDS", 300)))
    count = 0
    for action in list(actions):
        # Repeat the whole due predicate in the UPDATE, so the statement is a
        # compare-and-set rather than a blind write. Two schedulers that scanned
        # the same action both match on primary key and state; only one can
        # match on "still due", and the loser must not enqueue — nor stamp its
        # own hold over a later one another process has already set.
        held = AutomationAction.objects.filter(
            Q(paused_until=None) | Q(paused_until__lte=timestamp),
            Q(next_attempt_at=None) | Q(next_attempt_at__lte=timestamp),
            pk=action.pk,
            state=PENDING,
            finished__isnull=True,
        ).update(next_attempt_at=hold_until)
        if not held:
            continue  # Claimed, finished, or revived by another scheduler.
        enqueue_action(action.pk)
        count += 1
    return count


def cancel_instance(instance_id: int, message: str = "Canceled") -> int:
    """Cancel a running instance and every unfinished action inside it.

    Idempotent, and safe to call while workers are running: a worker that has
    already claimed an action finishes it, but nothing further is scheduled
    because the instance status is checked at the start of every execution.

    :returns: The number of actions canceled.
    """
    instance = AutomationInstance.objects.filter(pk=instance_id).first()
    if instance is None:
        return 0
    canceled = instance.cancel(message=message)
    logger.info(
        "automation.instance.canceled",
        extra={"automation_instance_id": instance_id, "actions_canceled": canceled},
    )
    return canceled


def _recovery_policy(action: AutomationAction, plugin_maps: dict):
    """Resolve the retry policy of a stranded action's plugin.

    The scheduler has no plugin instance to hand, so it rebuilds the tree the
    action belongs to. Falls back to the default policy when the plugin has been
    removed from the automation since the action was created — the action is
    about to fail for that reason anyway.
    """
    content_id = action.automation_instance.automation_content_id
    if content_id not in plugin_maps:
        try:
            plugin_maps[content_id] = build_plugin_map(content_id)
        except Exception:
            logger.exception("automation.recovery.plugin_map_failed", extra={"automation_action_id": action.pk})
            plugin_maps[content_id] = {}
    plugin = plugin_maps[content_id].get(action.plugin_ptr)
    if plugin is None:
        return DEFAULT_RETRY_POLICY
    action._plugin = plugin
    return get_retry_policy(action)


def recover_expired_leases(timestamp: datetime.datetime | None = None, limit: int = 500) -> int:
    """Recover actions whose worker died or which ran past their timeout.

    An action is recoverable when it is ``RUNNING`` and either exceeded its
    ``timeout_seconds`` or stopped refreshing its heartbeat for longer than
    ``AUTOMATION_LEASE_SECONDS``. It is rescheduled if attempts remain and
    failed otherwise, so a killed worker can never leave an execution stuck in
    ``RUNNING`` forever.

    :returns: The number of actions recovered.
    """
    timestamp = timestamp or now()
    window = getattr(settings, "AUTOMATION_LEASE_SECONDS", 300)
    horizon = timestamp - datetime.timedelta(seconds=window)
    # Prefilter cheaply in SQL, then let ``is_lease_expired`` decide exactly.
    # Actions carrying their own timeout are always considered: the timeout may
    # be far shorter than the lease window, so a stale-heartbeat filter alone
    # would miss an action that is running long but heartbeating happily.
    candidates = (
        AutomationAction.objects.select_related("automation_instance")
        .filter(
            state=RUNNING,
            finished__isnull=True,
        )
        .filter(
            Q(heartbeat_at__lte=horizon)
            | Q(heartbeat_at__isnull=True, started__lte=horizon)
            | Q(timeout_seconds__isnull=False)
        )[:limit]
    )

    recovered = 0
    # Resolving a plugin means building its whole plugin tree, so cache the maps
    # by automation content: a batch of stranded actions usually shares a few.
    plugin_maps: dict[int, dict] = {}
    for candidate in list(candidates):
        transitioned, reason = _recover_one(candidate.pk, timestamp, plugin_maps)
        if transitioned is None:
            continue
        recovered += 1
        logger.warning(
            "automation.action.recovered",
            extra={
                "automation_action_id": transitioned.pk,
                "reason": reason,
                "attempt": transitioned.attempt_count,
            },
        )
    return recovered


def _recover_one(action_id: int, timestamp: datetime.datetime, plugin_maps: dict):
    """Recover one stranded action, re-checking its lease under a row lock.

    The candidate scan is unlocked, so by the time an action is reached its
    worker may have heartbeated and be perfectly healthy again. Recovering it on
    the strength of that stale snapshot would take the action away from a
    running worker and start a duplicate — exactly what the lease prevents
    everywhere else. So the row is locked and expiry re-checked inside the same
    transaction as the transition; a heartbeat arriving concurrently blocks on
    that lock and is either already applied (we skip) or applied after (it finds
    the action no longer ``RUNNING`` under its lease, and does nothing).

    :returns: ``(transitioned_action_or_None, reason)``.
    """
    with transaction.atomic():
        action = (
            AutomationAction.objects.select_for_update()
            .select_related("automation_instance")
            .filter(pk=action_id)
            .first()
        )
        if action is None or action.state != RUNNING or action.finished is not None:
            return None, ""
        if not action.is_lease_expired(timestamp):
            return None, ""  # A heartbeat landed after the scan: the worker is alive.

        # Use the plugin's own retry policy, not just the persisted attempt
        # budget: its backoff, multiplier, cap and jitter shape how a recovered
        # action is rescheduled, and a crash is no reason to ignore them.
        policy = _recovery_policy(action, plugin_maps)
        max_attempts = effective_max_attempts(action, policy)
        timed_out = bool(
            action.timeout_seconds
            and action.started
            and (timestamp - action.started).total_seconds() > action.timeout_seconds
        )
        reason = "timed out" if timed_out else "lease expired"

        if action.attempt_count < max_attempts:
            due = timestamp + datetime.timedelta(seconds=policy.next_delay(action.attempt_count))
            transitioned = transition_action(
                action.pk,
                PENDING,
                allowed_from=(RUNNING,),
                message=f"Recovered: {reason}, retrying"[:MAX_FIELD_LENGTH],
                metadata={"recovery": reason},
                field_updates={"next_attempt_at": due, "paused_until": due},
                require_lease=action.lease_id,
            )
        else:
            transitioned = transition_action(
                action.pk,
                FAILED,
                allowed_from=(RUNNING,),
                result={"error": f"Action {reason}"},
                message=f"Recovered: {reason}, no attempts left"[:MAX_FIELD_LENGTH],
                metadata={"recovery": reason},
                require_lease=action.lease_id,
            )
        if transitioned is not None and transitioned.state == FAILED:
            # Inside the transaction: a FAILED action committed without its
            # dead-letter mark and instance failure cannot be repaired, because
            # the next recovery pass skips terminal actions entirely. The
            # ancestor walk always locks child before parent, so the consistent
            # ordering keeps it deadlock-free.
            dead_letter(transitioned)
            propagate_failure(transitioned)
        return transitioned, reason


def reconcile_waiting_joins(limit: int = 500) -> int:
    """Wake join points whose children all finished but which never got the news.

    ``notify_parent`` and ``_wake_if_children_done`` close the ordinary races,
    but a process that dies between a child finishing and its parent being
    notified leaves the parent ``WAITING`` with nothing left to wake it. This
    is the scheduler's backstop: it finds those parents and resumes them.

    :returns: The number of joins woken.
    """
    stuck = (
        AutomationAction.objects.filter(state=WAITING, finished__isnull=True, children__isnull=False)
        .exclude(children__finished__isnull=True)
        .exclude(automation_instance__status=CANCELED)
        .distinct()[:limit]
    )
    woken = 0
    for action in list(stuck):
        resumed = transition_action(
            action.pk,
            PENDING,
            allowed_from=(WAITING,),
            continuation=True,
            metadata={"woken_by": "join_reconciliation"},
        )
        if resumed is not None:
            enqueue_action(action.pk)
            woken += 1
            logger.warning(
                "automation.action.join_reconciled",
                extra={"automation_action_id": action.pk},
            )
    return woken


def _reopen_ancestors(action: AutomationAction, replacement_id: int) -> int:
    """Return the failed ancestors of a replayed action to ``WAITING``.

    Walks up the parent chain, reopening each ancestor that fail-fast
    propagation closed, so the replacement's eventual completion can wake the
    join. Ancestors that are still unfinished, or that failed on their own
    account rather than through propagation, are left alone.

    :returns: The number of ancestors reopened.
    """
    reopened = 0
    parent_id = action.parent_id
    while parent_id:
        parent = AutomationAction.objects.filter(pk=parent_id).first()
        if parent is None:
            break
        if parent.state == FAILED:
            transitioned = transition_action(
                parent.pk,
                WAITING,
                allowed_from=(FAILED,),
                message="Reopened for replay"[:MAX_FIELD_LENGTH],
                metadata={"reopened_for": replacement_id},
                field_updates={"dead_lettered": False, "dead_lettered_at": None, "finished": None},
            )
            if transitioned is None:
                break
            reopened += 1
        parent_id = parent.parent_id
    return reopened


def reconcile_stalled_instances(timestamp: datetime.datetime | None = None, limit: int = 500) -> int:
    """Close runs that have nothing left to do but were never finished.

    The engine commits each action's outcome together with what follows it, so
    this should find nothing. It exists for the paths that cannot take one
    transaction — lease recovery deliberately propagates failure outside the row
    lock — and as a backstop for anything unforeseen: an instance with no
    unfinished actions has no worker coming for it and no successor to make
    progress, so without this it would sit ``RUNNING`` forever.

    A grace period keeps it from racing live work: an instance is only
    considered once its most recent action has been finished for longer than the
    lease window, by which time any in-flight continuation has committed or
    rolled back.

    :returns: The number of instances closed.
    """
    timestamp = timestamp or now()
    grace = datetime.timedelta(seconds=float(getattr(settings, "AUTOMATION_LEASE_SECONDS", 300)))
    settled_before = timestamp - grace

    stalled = (
        AutomationInstance.objects.filter(finished__isnull=True, status=RUNNING)
        .exclude(automationaction__finished__isnull=True)
        .annotate(last_finished=models.Max("automationaction__finished"))
        .filter(last_finished__isnull=False, last_finished__lte=settled_before)
        .distinct()[:limit]
    )

    closed = 0
    for instance in list(stalled):
        # A run whose actions ended badly is failed, not completed.
        ended_badly = AutomationAction.objects.filter(
            automation_instance=instance, state__in=(FAILED, CANCELED)
        ).exists()
        status = FAILED if ended_badly else COMPLETED
        if transition_instance(
            instance.pk,
            status,
            allowed_from=(RUNNING,),
            unfinished_only=True,
            metadata={"reconciled": "no unfinished actions remained"},
        ):
            closed += 1
            logger.warning(
                "automation.instance.reconciled",
                extra={"automation_instance_id": instance.pk, "status": status},
            )
    return closed


def replay_action(action_id: int) -> AutomationAction | None:
    """Replay a dead-lettered action as a new attempt in its instance.

    Historical rows are never mutated: a fresh action is created linked to the
    original through ``replayed_from``, seeded with the input the failed attempt
    actually received, and its instance is reopened.

    Ancestors are the exception, and they have to be. Fail-fast propagation marks
    every unfinished ancestor ``FAILED`` when a branch dies, and ``notify_parent``
    only wakes a ``WAITING`` parent. Replaying a branch action while its parents
    stayed terminal would therefore complete the replacement and then stall: no
    join would ever fire and the reopened instance would run forever. So the
    ancestor chain is returned to ``WAITING`` — reopening the path the
    replacement needs in order to report back.

    :returns: The new action, or ``None`` if the original cannot be replayed.
    """
    original = AutomationAction.objects.filter(pk=action_id).select_related("automation_instance").first()
    if original is None or original.state not in (FAILED, CANCELED):
        return None

    # Most of what an action needs is its input, which is seeded below. A node
    # whose instruction lives on itself rather than in the data says so, and
    # says which part of it survives a replay.
    plugin = build_plugin_map(original.automation_instance.automation_content_id).get(original.plugin_ptr)
    carried = plugin.scratch_for_replay(dict(original.scratch or {})) if plugin is not None else {}

    with transaction.atomic():
        replacement = AutomationAction.objects.create(
            previous=original.previous,
            parent=original.parent,
            automation_instance=original.automation_instance,
            plugin_ptr=original.plugin_ptr,
            max_attempts=original.max_attempts,
            replayed_from=original,
            scratch=carried,
            finished=None,
        )
        _reopen_ancestors(original, replacement.pk)
        # The only path that moves a run out of a terminal status, so it goes
        # through the service like everything else — and leaves a record saying
        # which replay reopened it.
        transition_instance(
            original.automation_instance_id,
            RUNNING,
            metadata={"reopened_by_replay_of": original.pk, "replacement_action": replacement.pk},
        )
    logger.info(
        "automation.action.replayed",
        extra={"automation_action_id": replacement.pk, "replayed_from": original.pk},
    )
    enqueue_action(replacement.pk, data=original.input_data)
    return replacement


class scheduler_lock:
    """Hold a named scheduler lock for the duration of a block.

    Falsy when the lock could not be taken, so a second scheduler skips the
    tick instead of duplicating work::

        with scheduler_lock("runautomations") as held:
            if not held:
                return
    """

    def __init__(self, name: str = "runautomations", ttl_seconds: int = 300):
        self.name = name
        self.ttl_seconds = ttl_seconds
        self.token = None

    def __enter__(self):
        self.token = SchedulerLock.acquire(self.name, self.ttl_seconds)
        return self

    def __bool__(self) -> bool:
        return self.token is not None

    def __exit__(self, exc_type, exc, tb):
        if self.token is not None:
            SchedulerLock.release(self.name, self.token)
        return False


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
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _occurrences(config: dict) -> int:
    """How many occurrences of a recurring timer have elapsed, fired or skipped."""
    elapsed = config.get("occurrence_count")
    if elapsed is None:
        elapsed = config.get("fired_count", 0)
    return int(elapsed)


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
    if count_limit:
        # ``recurrence_count`` limits how many occurrences the schedule has, not
        # how many happened to execute: an occurrence skipped for being stale
        # still used one up. ``occurrence_count`` tracks that, falling back to
        # ``fired_count`` for triggers configured before the two diverged.
        elapsed = config.get("occurrence_count")
        if elapsed is None:
            elapsed = config.get("fired_count", 1)
        if int(elapsed) >= int(count_limit):
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


def fire_due_timers(timestamp: datetime.datetime | None = None, catch_up: int | None = None) -> int:
    """Fire all due timer triggers on active automations.

    A one-shot timer fires once when ``scheduled_at`` is reached; recurring
    timers step forward by their configured frequency/interval (simple
    wall-clock stepping — DST-exact recurrence is out of scope). The last
    fire time and count are stamped back into the trigger config.

    If the scheduler was down, a recurring timer may have missed several
    occurrences. Two bounds keep the recovery from becoming a storm:

    * ``catch_up`` (``AUTOMATION_TIMER_CATCHUP``, default 1) caps how many
      missed occurrences one tick fires. The default drains a backlog at one
      occurrence per tick, which is the historical behavior; raising it drains
      faster after a long outage.
    * ``AUTOMATION_TIMER_MAX_AGE`` (default ``None``) skips occurrences older
      than that many seconds instead of firing them, so a scheduler returning
      after a week does not replay a week of work. Skipped occurrences are
      logged and counted, never silently dropped.

    :returns: The number of triggers fired.
    """
    from .models import AutomationTrigger

    timestamp = timestamp or now()
    if catch_up is None:
        catch_up = int(getattr(settings, "AUTOMATION_TIMER_CATCHUP", 1))
    catch_up = max(1, catch_up)
    max_age = getattr(settings, "AUTOMATION_TIMER_MAX_AGE", None)

    fired = 0
    triggers = AutomationTrigger.objects.filter(
        type="timer", automation_content__automation__is_active=True
    ).select_related("automation_content")
    for trigger in triggers:
        config = dict(trigger.config or {})
        fired_here = skipped = 0
        while fired_here < catch_up:
            due = _next_timer_fire(config, timestamp)
            if due is None:
                break
            stale = max_age is not None and (timestamp - due).total_seconds() > float(max_age)
            # Read the elapsed count before touching either counter.
            # ``_occurrences`` falls back to ``fired_count`` when no
            # ``occurrence_count`` has been written yet, so reading it after
            # incrementing ``fired_count`` counts the same occurrence twice.
            elapsed = _occurrences(config)
            if stale:
                # Count it even though it did not run. ``recurrence_count``
                # limits how many occurrences the schedule *has*, not how many
                # happened to execute — otherwise a timer limited to two could
                # skip a week of stale slots and then fire two fresh ones, long
                # after its finite schedule should have ended.
                skipped += 1
                config["last_fired"] = due.isoformat()
                config["occurrence_count"] = elapsed + 1
            else:
                # Firing and recording the occurrence commit together, so a
                # scheduler that dies between them cannot fire it twice. The
                # idempotency key is belt and braces: derived from the trigger
                # and the occurrence itself, it makes a repeat delivery a no-op
                # even if the config write is somehow lost.
                with transaction.atomic():
                    trigger.trigger_execution(
                        data=[{"scheduled_at": due.isoformat(), "fired_at": timestamp.isoformat()}],
                        idempotency_key=f"timer:{trigger.pk}:{due.isoformat()}",
                    )
                    config["last_fired"] = due.isoformat()
                    config["fired_count"] = int(config.get("fired_count", 0)) + 1
                    config["occurrence_count"] = elapsed + 1
                    trigger.config = config
                    trigger.save(update_fields=["config"])
                fired_here += 1
            if skipped > 10000:  # pathological config guard
                break
        if skipped:
            trigger.config = config
            trigger.save(update_fields=["config"])
        if skipped:
            logger.warning(
                "automation.timer.occurrences_skipped",
                extra={"trigger_id": trigger.pk, "skipped": skipped, "max_age": max_age},
            )
        fired += fired_here
    return fired

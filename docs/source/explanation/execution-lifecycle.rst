.. _automation-execution-lifecycle:

Automation execution and reliability
====================================

An automation is not executed as one long function call. It is represented by
persisted state and advanced one action at a time. This is what lets execution
survive process restarts, wait for branches or people, retry transient failures,
and remain consistent when several workers act concurrently.

This page explains that lifecycle and the consistency guarantees around it.

Two levels of runtime state
---------------------------

An :class:`~djangocms_automation.instances.AutomationInstance` represents one
complete run of an automation. It stores the run's initial data, current data,
overall status, and finish time.

An :class:`~djangocms_automation.instances.AutomationAction` represents one
node in that run. Actions carry the finer-grained execution state: input and
result data, attempts, timing, retry schedule, errors, and the lease identifying
the worker that currently owns the action. Every normal state change also
creates an immutable
:class:`~djangocms_automation.instances.AutomationActionEvent`, providing an
audit trail independently of the action's current state.

The action state machine is:

.. code-block:: text

                         retry or pause
                    +----------------------+
                    |                      |
                    v                      |
    PENDING --claim--> RUNNING --wait--> WAITING --wake--> PENDING
                         |
                         +--success-----------------------> COMPLETED
                         |
                         +--terminal failure-------------> FAILED
                                                             |
                                    reopened for replay <----+
                                    (back to WAITING, so the
                                     join can be woken again)

    Any unfinished action may be canceled ----------------> CANCELED

This is not the authority, only a picture of it. The lifecycle is declared as
data in :data:`~djangocms_automation.instances.ALLOWED_TRANSITIONS` and enforced
on every state change: a transition the table does not list raises
``InvalidTransition`` at the call site rather than quietly writing a state
nothing can act on. Read that table when this diagram and the code disagree.

Note the distinction it draws. A transition that is *legal but refused* — the
wrong source state, a lost lease, an action already finished — returns ``None``
quietly, because those are ordinary races and absorbing them is what makes
duplicate delivery harmless. A transition that is *impossible* is a programming
error and says so.

``COMPLETED`` and ``CANCELED`` are terminal. ``FAILED`` is terminal in every
path except replay, which reopens a failed ancestor so a replayed branch can
report back to its join.

The run itself has the same treatment, one level up. An instance starts
``RUNNING`` and ends in one of the three terminal statuses, declared in
:data:`~djangocms_automation.instances.ALLOWED_INSTANCE_TRANSITIONS` and applied
by ``transition_instance``. The edges worth attention are the ones pointing
*back*: replay is the only thing that moves a run out of a terminal status, and
it now says so, recording which replay reopened the run and clearing
``finished`` so the run is genuinely open rather than merely relabelled. Every
instance status change leaves an
:class:`~djangocms_automation.instances.AutomationInstanceEvent`. ``PENDING``
means that an action is available now or scheduled for later; ``RUNNING`` means
that a worker owns an execution lease; and ``WAITING`` means that progress
depends on child branches or human input rather than worker time.

From trigger to execution
-------------------------

A trigger creates an instance and its first ``PENDING`` action. Enqueuing is
deferred until the surrounding database transaction commits, so a worker never
observes an action whose creation is later rolled back. With the durable
database backend, the queued task is itself persisted and can survive worker or
web-process restarts.

A worker does not execute a task merely because it received the message. It
first claims the action through an atomic ``PENDING`` to ``RUNNING`` transition.
The claim:

* takes a database row lock;
* verifies that the action is still pending and unfinished;
* increments the attempt or re-entry counter;
* assigns a new, unique ``lease_id``;
* records ``started`` and ``heartbeat_at``; and
* stores the input, timeout, and retry budget needed for recovery.

Those fields are committed together. A second delivery of the same queued task
therefore finds an action that is no longer ``PENDING`` and becomes a no-op.

Successful execution
--------------------

The plugin receives normalized data rows and returns a state and output. For an
ordinary successful node, the engine records ``COMPLETED`` and creates the next
action or actions. The output becomes their input. When a completed root chain
has no successor and no unfinished actions remain, the instance is marked
``COMPLETED`` and its final data is stored.

Control-flow nodes take a longer path. A conditional or split enters
``WAITING`` while its selected branch or parallel paths execute. Child actions
refer back to that waiting parent. When the children finish, exactly one of
them changes the parent from ``WAITING`` to ``PENDING`` and enqueues it for
re-entry. The parent then joins the branch results and continues downstream.

Re-entry is deliberately distinct from retry. Re-entering a join, reviving a
deliberately paused action, or continuing after human input has not repeated a
failed attempt. It increments ``re_entry_count`` rather than consuming
``attempt_count``.

Failure and fail-fast propagation
---------------------------------

There are two broad failure paths:

* A plugin can explicitly return ``FAILED``. This is treated as a terminal
  outcome.
* A plugin can raise an exception. The engine consults the plugin's
  :class:`~djangocms_automation.retry.RetryPolicy` to decide whether the
  exception is retryable.

Retry classification is intentionally opt-in. A
:class:`~djangocms_automation.retry.RetryableError`, or an exception listed in
``retry_on``, may be retried. A
:class:`~djangocms_automation.retry.PermanentError` is never retried, and an
unknown exception fails immediately. Retrying every exception would be unsafe:
an action may already have performed part of an external side effect before it
raised.

When a failure is terminal, the action records its error type, detail, message,
and traceback before entering ``FAILED``. It is marked as dead-lettered for
inspection or replay. Failure then propagates through unfinished parent joins,
and the whole instance becomes ``FAILED``. This fail-fast rule avoids leaving a
branch parent waiting forever for a child that can no longer complete.

Failures in observability hooks are isolated. Transition, dead-letter, and
instance-finished signal receivers may log or report failures, but an exception
from a receiver does not interrupt the state transition it observes.

Retries, recovery, and replay
-----------------------------

A retry keeps the same action row and advances its attempt history. If the
exception is retryable and attempts remain, the action returns to ``PENDING``
with ``next_attempt_at`` and ``paused_until`` set from the plugin's retry
policy. Backoff can grow between attempts, include jitter, and be overridden by
``RetryableError.retry_after``. The scheduler later enqueues the action when it
is due, and the next claim increments ``attempt_count``.

Retries also cover work interrupted without a Python exception. While a plugin
runs, a background heartbeat refreshes its action lease. A transient database
failure does not stop heartbeat renewal: the heartbeat retries with capped
backoff and replaces a failed database connection. The scheduler considers a
``RUNNING`` action recoverable when its heartbeat is older than
``AUTOMATION_LEASE_SECONDS`` or its configured execution timeout has elapsed.

Recovery uses the same attempt budget and plugin-specific backoff as an
ordinary retry. If attempts remain, the action returns to ``PENDING``. If the
budget is exhausted, it becomes ``FAILED`` and is dead-lettered. The durable
task queue has its own lease as well: it releases a task row abandoned by a
dead worker, while action recovery repairs the corresponding workflow state.

Replay is different from retry. An operator replaying a dead-lettered action is
making a new execution decision, so the engine creates a new action linked by
``replayed_from`` and preserves the failed row as history. Failed ancestors are
reopened so the replacement can report back through its joins; superseded
children are excluded when those joins evaluate their result.

How racing conditions are contained
------------------------------------

Reliability depends on decisions being made atomically at the database boundary,
not on workers happening to run in a convenient order.

.. list-table:: Race and safeguard
   :header-rows: 1
   :widths: 36 64

   * - Possible race
     - Safeguard
   * - A task is delivered twice.
     - Claiming locks the action row and permits only ``PENDING`` to
       ``RUNNING``. Only one worker receives a lease.
   * - A worker finishes after its lease was recovered and another attempt
       started.
     - Worker-originated success, failure, pause, and retry transitions are
       fenced by the lease captured at claim time. A stale lease cannot write
       into the newer attempt.
   * - A heartbeat arrives while the scheduler is deciding that an action is
       abandoned.
     - Recovery locks and reloads the action, then checks expiry again inside
       the same transaction as the recovery transition. A decision is never
       made from the earlier candidate snapshot alone.
   * - Several branch children finish together.
     - Waking the parent is an atomic ``WAITING`` to ``PENDING`` transition.
       Only the winning child enqueues it.
   * - A child finishes just before its parent enters ``WAITING``, or a process
       dies between child completion and notification.
     - A post-transition lost-wakeup check handles the first window; scheduler
       reconciliation repairs waiting joins whose children are already done.
   * - Several scheduler processes start the same tick.
     - A database-backed scheduler lock lets one process recover actions, fire
       timers, and revive due work while the others skip the tick.
   * - Several callers submit the same triggering event.
     - When supplied, the trigger's ``idempotency_key`` is protected by a
       database uniqueness constraint and creates at most one instance for that
       automation content.
   * - Several paths try to finish the same instance.
     - Instance completion updates only a row whose ``finished`` value is still
       null, so terminal completion and its signal occur once.

The practical guarantee: at-least-once effects
----------------------------------------------

Leases and row locks protect orchestration state; they cannot turn an arbitrary
external side effect into an exactly-once operation. A worker can send an email
or submit an API request and then die before recording ``COMPLETED``. Recovery
must retry because the database cannot know whether that external operation
happened. A configured timeout can likewise expire while non-interruptible
plugin code is still returning.

Action implementations should therefore make externally visible work
idempotent wherever repetition would be harmful. Typical strategies are a
stable business key, an idempotency key accepted by the remote API, or a local
outbox/uniqueness record committed with the automation state. Trigger
idempotency prevents duplicate *instances*; it does not by itself make every
action inside an instance exactly-once.

This boundary is why retry classification is conservative and why replay is an
explicit operator action: the engine prevents stale workers and duplicate task
deliveries from corrupting workflow state, while action authors retain control
over whether an external effect is safe to repeat.

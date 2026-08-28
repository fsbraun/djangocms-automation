Admin Reference
===============

The package adds several entries under **Automations** in the Django admin.
Some are places to author things, some exist only to inspect what happened, and
two are runtime plumbing you will normally leave alone. This page explains what
each one is for and what its permissions actually grant.

Overview
--------

.. list-table::
   :header-rows: 1
   :widths: 24 12 64

   * - Entry
     - You can
     - Purpose
   * - :ref:`admin-automations`
     - add, change
     - Author workflows. The main entry point.
   * - :ref:`admin-triggers`
     - *(hidden)*
     - How an automation starts. Edited from the workflow, not the index.
   * - :ref:`admin-secrets`
     - add, change
     - API keys for external services, e.g. LLM providers.
   * - :ref:`admin-instances`
     - add, change
     - One row per execution. Inspect, resume, cancel.
   * - :ref:`admin-events`
     - view
     - Immutable audit trail of every state change.
   * - :ref:`admin-dead-letters`
     - view
     - Failed actions awaiting inspection or replay.
   * - :ref:`admin-queued-tasks`
     - view
     - The durable task queue, for watching backlog.
   * - :ref:`admin-scheduler-locks`
     - change
     - Which scheduler currently holds the tick.

Authoring
---------

.. _admin-automations:

Automations
~~~~~~~~~~~

The workflows themselves. Each automation is a grouper with versioned content,
following the django CMS pattern: adding one creates a draft you then edit.

The admin is where you create an automation, set its name, and switch it active
or inactive. **An inactive automation is not scheduled**: its timers do not fire
and its pending actions are not revived, though runs already in flight finish.
That makes the active flag the safe way to stop a misbehaving workflow without
deleting anything.

The workflow itself — triggers, conditions, splits, actions — is built in the
frontend editor, not here. Use the automation's preview/edit link to open it.

.. _admin-triggers:

Triggers
~~~~~~~~

How an automation starts: manual, timer, webhook, mail, form submission, or from
another automation. Each trigger type contributes its own configuration fields.

Triggers are **deliberately hidden from the admin index**. They only make sense
in the context of one automation, so they are added and edited from the
automation editor — the *Automation → Triggers* toolbar menu, or by clicking a
trigger node in the workflow. The permissions still exist and are still
enforced, which is why ``djangocms_automation.change_automationtrigger`` appears
when you assign permissions even though there is no index link.

The same applies to *automation contents*, the versioned content objects behind
each automation; django CMS versioning manages those for you.

.. _admin-secrets:

Secrets
~~~~~~~

API keys for external services, stored per service id (``anthropic``,
``openai``, ``google``, …). The LLM action looks up an **active** key by the
provider prefix of the model string, so ``anthropic/claude-opus-4-8`` requires
an active ``anthropic`` key.

Keys are shown masked in the change form. Deactivate rather than delete when
rotating: an automation that runs mid-rotation then fails with a clear
configuration error instead of a missing-row error.

Inspecting a run
----------------

.. _admin-instances:

Execution Instances
~~~~~~~~~~~~~~~~~~~

One row per execution of an automation, with the data it started from, the data
it currently holds, and its status. Each instance lists its actions inline —
state, attempts, re-entries, timings — which is the quickest way to see where a
run got to.

Two operations live here:

*Cancel selected executions*
    Stops a run and every unfinished action in it. Idempotent, and safe while
    workers are running: an action already claimed finishes, but nothing further
    is scheduled, because the engine re-checks instance status after claiming.

*Open tasks*
    A separate view listing actions waiting for a human, filtered to the ones
    you are permitted to resume. This is where *Wait for User* actions surface.

Add and change permissions exist because it is a normal model, but creating an
execution by hand is not a supported way to run an automation — use a trigger.

.. _admin-events:

Action events
~~~~~~~~~~~~~

An immutable record of every action state transition: from-state, to-state,
attempt number, lease id, and any metadata the engine attached (why a retry was
scheduled, which child woke a join, which user resumed a task).

View-only by design — adding or editing rows would corrupt the audit trail. This
is the place to answer "what actually happened, in what order" for a run that
behaved unexpectedly.

Recovering from failure
-----------------------

.. _admin-dead-letters:

Dead letters
~~~~~~~~~~~~

Actions that failed terminally. Every terminal failure lands here, not only ones
that exhausted a retry budget, because replay is just as useful for an action
that never had one.

Each entry keeps the input the failed attempt actually received, its error type
and detail, its attempt history, and a link to any replays. The single operation
is *Replay selected actions*, which creates a **new** action seeded with that
stored input and reopens the instance. Historical rows are never modified, so a
replay is auditable: the new action links back through ``replayed_from``.

Replay uses the currently published workflow definition, not the one that was
live when the run failed. If the workflow changed in between, the replay follows
the new one.

.. note::
   Deleting a dead letter destroys the record of a failure, including its
   attempt history and error detail. Prefer replaying, or leave it as history.

Runtime plumbing
----------------

These two reflect the state of the machinery rather than anything you author.
They are useful when automations are not progressing and you need to know
whether the workers and scheduler are healthy.

.. _admin-queued-tasks:

Queued tasks
~~~~~~~~~~~~

The durable queue behind
:class:`~djangocms_automation.backends.DatabaseBackend` — one row per enqueued
task, with its state, queue, attempts, owning worker, and any traceback from a
worker-side failure.

Mainly a monitoring surface. A growing number of rows in ``READY`` means work is
arriving faster than the workers drain it, or no worker is running at all. Rows
stuck in ``RUNNING`` past their claim expiry are released automatically by the
next worker or scheduler tick.

Only present when the database-backed task backend is configured.

.. warning::
   Deleting a task row in ``READY`` silently drops queued work: the action it
   would have executed stays ``PENDING`` and nothing re-enqueues it until the
   scheduler's next revive pass. Do not use deletion to clear a backlog.

.. _admin-scheduler-locks:

Scheduler locks
~~~~~~~~~~~~~~~

A single short-lived row per lock name, showing which scheduler process holds
the current tick and until when. It exists so ``runautomations`` can be
installed on several hosts without firing timers twice.

Normally empty or briefly populated. A lock that stays held long past its expiry
means a scheduler died mid-tick; the next tick reclaims it automatically once
``locked_until`` passes, so intervention is rarely needed. Clearing it by hand
is the escape hatch if you need the next tick to run immediately.

Permissions
-----------

All entries use standard Django model permissions, so they can be assigned per
group in the usual way. Two notes:

- Hidden entries still enforce permissions. A user who may open the automation
  editor also needs ``add_automationtrigger`` / ``change_automationtrigger`` to
  add or edit triggers there; without them the toolbar shows the trigger entries
  disabled.
- Resuming a *Wait for User* action is governed by the permissions configured on
  that action, not by admin permissions. Superusers can always resume.

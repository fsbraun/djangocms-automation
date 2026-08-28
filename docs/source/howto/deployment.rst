Deploying automations in production
===================================

Automations execute outside the request/response cycle. A production deployment
therefore runs three kinds of process, and the difference between them is the
difference between automations that survive a restart and automations that do
not.

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Process
     - Responsibility
   * - Web
     - Serves the site and the editor. Enqueues work; never executes it.
   * - Worker
     - Claims queued tasks and runs actions. Run one or more.
   * - Scheduler
     - Fires timers, revives due retries, recovers dead workers, applies
       retention. Runs periodically.

Choosing a task backend
-----------------------

Django's task framework ships two backends, neither suitable for production
automations:

``ImmediateBackend``
    Executes inline, inside the request. A slow action blocks the response, and
    nothing survives a crash. Useful in tests.

``DummyBackend``
    Records tasks and never runs them.

This package adds two more:

:class:`djangocms_automation.utils.ThreadBackend`
    Runs tasks in a bounded in-process thread pool. Non-durable: work and
    results are process-local and lost on restart or termination. It has no
    retry or crash recovery. **Development only.**

:class:`djangocms_automation.backends.DatabaseBackend`
    Durable. Enqueued work is a database row, claimed under a lease and
    executed by a separate worker process. Survives restarts; a killed worker's
    task is released and retried.

A backend is production-capable for automations when it provides durable
storage of enqueued work, execution in a separate process from the web server,
at-least-once delivery, and visibility for work that failed to enqueue. Any
backend meeting that contract works; nothing in the engine depends on which one
you choose.

Configuration
-------------

.. code-block:: python

    TASKS = {
        "default": {
            "BACKEND": "djangocms_automation.backends.DatabaseBackend",
            "QUEUES": ["default"],
        }
    }

Running the processes
---------------------

.. code-block:: bash

    # Web
    gunicorn myproject.wsgi

    # Worker — run at least one; add more to increase throughput
    python manage.py runworker

    # Scheduler — every minute
    * * * * * cd /path/to/project && python manage.py runautomations

The worker accepts ``--queue`` (repeatable), ``--sleep``, ``--lease-seconds``,
``--max-tasks`` (exit after N tasks, to recycle long-lived processes) and
``--once`` (drain what is ready and exit, for CI).

Graceful shutdown
-----------------

The worker traps ``SIGINT`` and ``SIGTERM``, finishes the task in flight, and
exits. Give your process manager a stop timeout longer than your longest action
so a deploy does not kill work mid-execution — and set
``AUTOMATION_ACTION_TIMEOUT`` so "longest action" is a number you chose rather
than one you discovered.

Work that is interrupted anyway is not lost: the scheduler's recovery pass
returns it to the queue.

Scaling and availability
------------------------

Run as many workers as you need; claiming is exclusive, so they never duplicate
work. The scheduler may also be installed on several hosts — it takes a
database-backed lock, so exactly one host performs each tick and the others skip
it. There is no separate coordinator to deploy.

Health and readiness
--------------------

Signals worth alerting on:

* actions ``RUNNING`` for longer than ``AUTOMATION_LEASE_SECONDS`` — a worker
  is gone, or the scheduler is not running;
* a growing ``QueuedTask`` backlog in the ``READY`` state — too few workers;
* any dead-lettered action — a failure a human should look at;
* a scheduler tick that has not run in several minutes.

The last one deserves its own alert. Nothing else recovers dead workers, fires
timers, or drains retries: if the scheduler stops, automations quietly stop
making progress while everything else looks healthy.

Settings
--------

.. list-table::
   :header-rows: 1
   :widths: 35 12 53

   * - Setting
     - Default
     - Meaning
   * - ``AUTOMATION_LEASE_SECONDS``
     - ``300``
     - How long an action may go without a heartbeat before it is considered
       abandoned and recovered.
   * - ``AUTOMATION_ACTION_TIMEOUT``
     - ``None``
     - Default per-action execution limit in seconds. A plugin may override it
       with its own ``timeout_seconds``.
   * - ``AUTOMATION_TIMER_CATCHUP``
     - ``1``
     - Missed timer occurrences fired per scheduler tick. The default drains a
       backlog gradually; raise it to recover faster after an outage.
   * - ``AUTOMATION_TIMER_MAX_AGE``
     - ``None``
     - Skip occurrences older than this many seconds instead of firing them, so
       a scheduler returning after a long outage does not replay ancient work.

Retention
---------

Execution history accumulates. The scheduler applies retention when asked:

.. code-block:: bash

    # Delete finished executions older than 30 days
    python manage.py runautomations --cleanup 30

    # Or keep the audit trail and drop only the payloads
    python manage.py runautomations --redact 30

Redaction keeps states, timings, attempts and the event history while removing
the data that flowed through the run — the right default where payloads carry
personal data but the audit trail must survive.

Only *finished* executions are eligible, so a run waiting on a human is never
removed however old it is.

Development
-----------

The repository ships a demo project that runs the full production shape —
durable backend, separate worker, scheduler — against SQLite:

.. code-block:: bash

    cd demo
    python manage.py migrate
    python manage.py seedautomations      # creates an admin user and reference automations
    python manage.py runserver
    python manage.py runworker            # in a second shell

``seedautomations --scenario`` injects failure conditions (``killed-worker``,
``timeout``, ``enqueue-rejection``, ``duplicate-webhook``, ``timer-backlog``) so
recovery can be watched rather than only trusted.

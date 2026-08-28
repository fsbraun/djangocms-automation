Installation
============

Downloading the package
-----------------------

Install the package from GitHub using pip:

.. code-block:: bash

    pip install git+https://github.com/fsbraun/djangocms-automation.git

Configuration
-------------

1. Add ``djangocms_automation`` to your ``INSTALLED_APPS``:

.. code-block:: python

    INSTALLED_APPS = [
        # ...
        "djangocms_automation",
        # ...
    ]

2. Run migrations:

.. code-block:: bash

    python manage.py migrate djangocms_automation

Migrations ``0010`` through ``0014`` together install the reliability schema.
They add action attempt, lease, error, recovery, replay, and cancellation
bookkeeping; action events and the scheduler lock; the dead-letter proxy; the
durable queued-task table; and instance events. Existing actions start with an
attempt count of zero, and the next successful claim creates their first
recorded attempt. Apply the complete migration sequence with ``migrate``; no
manual data backfill is required.

3. (Optional) Include the package URLs to enable inbound webhooks
   (see :doc:`../howto/webhooks`):

.. code-block:: python

    urlpatterns = [
        # ...
        path("automation/", include("djangocms_automation.urls")),
    ]

Running Automations
-------------------

Automations are executed via background tasks. You need to set up a periodic
task to process pending automations.

Using a cron job
^^^^^^^^^^^^^^^^

Add a cron job to run the ``runautomations`` management command periodically.
It revives paused/pending actions **and** fires due timer triggers. For
example, to run every minute:

.. code-block:: bash

    * * * * * cd /path/to/your/project && /path/to/venv/bin/python manage.py runautomations

Using Django-Q2
^^^^^^^^^^^^^^^

If you're using `Django-Q2 <https://django-q2.readthedocs.io/>`_, you can schedule the task:

.. code-block:: python

    from django_q.tasks import schedule

    schedule(
        "django.core.management.call_command",
        "runautomations",
        schedule_type="I",  # Interval
        minutes=1,
    )

Using Celery
^^^^^^^^^^^^

Create a periodic task in your `Celery <https://docs.celeryq.dev/>`_ configuration:

.. code-block:: python

    from celery import Celery
    from celery.schedules import crontab

    app = Celery()

    @app.on_after_configure.connect
    def setup_periodic_tasks(sender, **kwargs):
        sender.add_periodic_task(
            60.0,  # Run every 60 seconds
            run_pending_automations.s(),
        )

    @app.task
    def run_pending_automations():
        from django.core.management import call_command
        call_command("runautomations")

Using Django Background Tasks
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Django 6.0+ supports ``django.tasks`` for background task execution. The package
uses this natively via the ``@task`` decorator. Configure a task backend in your
settings:

.. code-block:: python

    TASKS = {
        "default": {
            "BACKEND": "django.tasks.backends.ImmediateBackend",
        }
    }

The immediate backend runs the automation inside the request/response cycle.
It is useful for tests, but a slow action will also make the request slow.

For local development, or for non-critical best-effort work, the package also
provides a bounded in-process thread backend:

.. code-block:: python

    TASKS = {
        "default": {
            "BACKEND": "djangocms_automation.utils.ThreadBackend",
            "OPTIONS": {
                "MAX_WORKERS": 4,
            },
        }
    }

``ThreadBackend`` returns from enqueueing promptly and executes the task in a
worker thread after the surrounding database transaction commits. It is not a
durable task queue: queued tasks and results exist only in the web process's
memory, are not shared between multiple processes, and are lost if that process
is restarted or terminated. The automation engine can re-enqueue persisted
``PENDING`` actions and recover expired action leases after a restart, but the
thread backend itself provides no durable queue, cross-process workers, or
persistent task status. Do not use it for production automation where losing or
delaying an email, database update, webhook, or paid external API call would be
unacceptable.

For production, use the package's durable database backend:

.. code-block:: python

    TASKS = {
        "default": {
            "BACKEND": "djangocms_automation.backends.DatabaseBackend",
            "QUEUES": ["default"],
        }
    }

Run one or more workers separately from the web process:

.. code-block:: bash

    python manage.py runworker

Automatic retry and recovery are built into the current release. A plugin's
:class:`~djangocms_automation.retry.RetryPolicy` controls which exceptions may
be retried, the attempt budget, and backoff. Running actions renew an execution
lease; if a worker disappears, the scheduler detects the expired lease and
either reschedules the action or dead-letters it when no attempts remain. The
database backend also persists queued tasks and releases queue claims abandoned
by dead workers.

Continue to run ``python manage.py runautomations`` periodically. It recovers
expired action leases, revives due retries and paused actions, reconciles joins,
and fires timer triggers. The worker and scheduler are both required for the
full production reliability model. See :doc:`../howto/deployment` for the
complete process layout and :doc:`../explanation/execution-lifecycle` for why
retries and recovery behave this way.

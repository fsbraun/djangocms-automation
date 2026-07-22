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

Release upgrades that include execution-attempt tracking apply migration
``0010_action_attempts_and_events``. It adds attempt and lease fields to
existing actions and creates their transition-event table. Existing actions
start with an attempt count of zero; the next successful claim creates their
first recorded attempt. No data backfill is required.

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
is restarted or terminated. It provides no retry or crash recovery. Do not use
it for production automation where losing an email, database update, webhook,
or paid external API call would be unacceptable.

The database now records task attempts and transition events, but this does not
make an in-memory backend durable. The current release does not automatically
retry failed actions or recover a task lost when its process exits.

For production, configure a durable Django task backend with persistent queue
storage and separate workers. The exact backend and worker command depend on
the task backend package you select. Independently of that worker, continue to
run ``python manage.py runautomations`` periodically so paused actions and timer
triggers are revived.

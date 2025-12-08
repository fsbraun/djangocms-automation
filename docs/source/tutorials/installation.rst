Installing djangocms-automation
===============================

Installation
------------

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

Running Automations
-------------------

Automations are executed via background tasks. You need to set up a periodic
task to process pending automations.

Using a cron job
^^^^^^^^^^^^^^^^

Add a cron job to run the pending automations periodically. For example, to run
every minute:

.. code-block:: bash

    * * * * * cd /path/to/your/project && /path/to/venv/bin/python manage.py shell -c "from djangocms_automation.tasks import execute_pending_automations; execute_pending_automations()"

Using Django-Q2
^^^^^^^^^^^^^^^

If you're using `Django-Q2 <https://django-q2.readthedocs.io/>`_, you can schedule the task:

.. code-block:: python

    from django_q.tasks import schedule

    schedule(
        "djangocms_automation.tasks.execute_pending_automations",
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
        from djangocms_automation.tasks import execute_pending_automations
        execute_pending_automations()

Using Django Background Tasks
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Django 6.0+ supports ``django.tasks`` for background task execution. The package
uses this natively via the ``@task`` decorator. Configure a task backend in your
settings:

.. code-block:: python

    TASKS = {
        "default": {
            "BACKEND": "django.tasks.backends.ImmediateBackend",
            # Or use a database backend for production
        }
    }

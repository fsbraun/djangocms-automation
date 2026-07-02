Triggering an automation
========================

Every automation has at least one **trigger**. Each trigger owns a
placeholder (its slot) holding the flow that runs when it fires. Trigger
types are registered in ``djangocms_automation.triggers.trigger_registry``.

Programmatically
----------------

Fetch the trigger and call
:meth:`~djangocms_automation.models.AutomationTrigger.trigger_execution`.
Data is a list of JSON-serializable rows:

.. code-block:: python

    from djangocms_automation.models import AutomationTrigger

    trigger = AutomationTrigger.objects.get(
        automation_content__automation__name="Welcome flow",
        slot="start",
    )
    trigger.trigger_execution(
        data=[{"first_name": "Alice", "email": "alice@example.com"}],
        start=True,  # enqueue immediately; False creates the run paused
    )

On form submission
------------------

With `djangocms-form-builder <https://github.com/fsbraun/djangocms-form-builder>`_
installed, a *Trigger automation* form action becomes available. Give the
automation a trigger of type *Form Submission*, then select the automation
in the form's action settings. On submit, the cleaned form data is
serialized to a data row (plus ``user_id``) and the automation starts.

On a schedule (timer)
---------------------

Create a trigger of type *Timer* and configure ``Scheduled at`` plus an
optional recurrence (hourly/daily/weekly/monthly with an interval, end date
or count). Due timers are fired by the ``runautomations`` management
command — run it periodically via cron:

.. code-block:: bash

    * * * * * cd /path/to/project && python manage.py runautomations

The fire time is stamped into the trigger config (``last_fired``,
``fired_count``); one-shot timers fire exactly once.

From another automation
-----------------------

Triggers of type *Automation* (``code``) mark entry points intended to be
started by other automations or custom code — call ``trigger_execution``
as shown above.

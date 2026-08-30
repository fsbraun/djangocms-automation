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
        start=True,  # enqueue immediately; False defers to the scheduler
    )

``start=False`` creates the instance and its first action but does not enqueue
the action immediately. It is not a pause: the action remains ``PENDING`` with
no ``paused_until`` value, so the next ``runautomations`` scheduler tick finds
and enqueues it. Use this option to defer startup to the scheduler, not to hold
an execution indefinitely.

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

Via webhook (HTTP POST)
-----------------------

Triggers of type *Webhook* (and *Mail*) expose a secret URL that external
services can POST JSON to. See :doc:`webhooks` for setup, signature
verification, mail ingestion, and writing custom webhook trigger types.

From another automation
-----------------------

Triggers of type *Automation* (``code``) mark entry points intended to be
started by other automations or custom code — call ``trigger_execution``
as shown above.

Declaring what it accepts
~~~~~~~~~~~~~~~~~~~~~~~~~

Every other trigger type knows its payload from the outside world it listens
to. This one is called from inside, so the shape is whatever the automation was
built to expect — which only its author can say. Set **Data schema** on the
trigger to write it down:

.. code-block:: json

    {
      "type": "object",
      "properties": {"email": {"type": "string"}},
      "required": ["email"],
      "additionalProperties": false
    }

Leaving it empty keeps the current behaviour: anything is accepted. Filling it
in makes the trigger check its payload, and gives a caller something to read
before calling — ``AutomationTrigger.data_schema`` returns it.

The field editor covers a flat record: name each field, choose its type, mark
required fields, and describe them for the next caller. **Edit as JSON** keeps
the existing escape hatch for schemas that use nesting or other advanced JSON
Schema features; such values are never simplified automatically.

``additionalProperties: false`` is required rather than encouraged, because
this schema is the trigger's answer to "what may I send you", and one that
permits anything extra does not answer it.

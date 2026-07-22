Engine Reference
================

.. automodule:: djangocms_automation.engine
   :members:
   :undoc-members:
   :show-inheritance:

The execution engine: claiming, dispatching, joining, failure propagation,
pausing/reviving and timer firing.

Action transitions
------------------

.. automodule:: djangocms_automation.transitions
   :members:
   :undoc-members:

The transition service serializes action state changes with a database row
lock. A successful claim increments ``attempt_count``, assigns ``lease_id``,
sets ``started`` and ``heartbeat_at``, and appends an
``AutomationActionEvent``. A claim whose expected source state no longer
matches returns ``None``; this makes duplicate task delivery a no-op.

``heartbeat_action`` is available for a worker that owns the current lease.
The engine does not yet emit periodic heartbeats or recover expired leases.
Those fields must not be interpreted as a complete retry or timeout system.

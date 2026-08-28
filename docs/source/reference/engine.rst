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

A claim distinguishes a **retry** from a **re-entry**. Re-entering a waiting
node — a split joining its branches, a paused action reviving — increments
``re_entry_count`` and leaves ``attempt_count`` untouched, because neither is a
failure and neither should consume the retry budget. Only a claim following a
failure counts as an attempt.

``heartbeat_action`` refreshes the lease for a worker that still owns it.
Expired leases are recovered by ``engine.recover_expired_leases``, which the
``runautomations`` scheduler calls each tick.

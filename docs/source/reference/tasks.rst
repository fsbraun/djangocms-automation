Tasks Module Reference
======================

.. automodule:: djangocms_automation.tasks
   :members:
   :undoc-members:
   :show-inheritance:

Task helpers for scheduling and executing automation actions are documented
here.

Thread backend
--------------

.. autoclass:: djangocms_automation.utils.ThreadBackend
   :members: enqueue, get_result

This backend uses a bounded thread pool in the current application process.
Set its concurrency with ``TASKS["default"]["OPTIONS"]["MAX_WORKERS"]``;
the default is 4. It implements Django's task result protocol, but its queue
and results are in-memory and non-durable. It is intended for development and
best-effort workloads, not reliable production execution.

Reliability Reference
=====================

Retry policy
------------

.. automodule:: djangocms_automation.retry
   :members:
   :undoc-members:
   :show-inheritance:

Failure classification is explicit. An unclassified exception is **not**
retried: repeating an unknown failure risks repeating a side effect that already
partially happened. Actions opt in by raising
:class:`~djangocms_automation.retry.RetryableError`, or by listing exception
classes in ``retry_on``. :class:`~djangocms_automation.retry.PermanentError`
overrides both.

Durable task backend
--------------------

.. automodule:: djangocms_automation.backends
   :members:
   :undoc-members:
   :show-inheritance:

.. automodule:: djangocms_automation.queue
   :members:
   :undoc-members:
   :show-inheritance:

Signals
-------

.. automodule:: djangocms_automation.signals
   :members:
   :undoc-members:

These are the supported extension points for metrics, tracing and alerting.
Receivers are called inside the engine but their exceptions are caught and
logged: an observability failure can never fail an execution.

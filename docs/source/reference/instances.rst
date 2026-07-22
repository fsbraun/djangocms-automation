Automation Instances
====================

.. automodule:: djangocms_automation.instances
   :members:
   :undoc-members:
   :show-inheritance:

This module contains ``AutomationInstance``, ``AutomationAction`` and
``AutomationActionEvent`` runtime classes used by the execution engine.

``AutomationAction`` represents one logical workflow step. Its attempt and
lease fields describe the most recent execution attempt; ``events`` provides
the ordered transition history. ``error_type`` and ``error_detail`` are safe
summary fields for filtering and display, while the action result may contain
the full traceback used for debugging.

The event history is deleted with its action. It is operational audit data,
not a replacement for an application-level compliance audit log.

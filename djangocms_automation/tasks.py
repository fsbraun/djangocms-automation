"""Task entry points for the automation engine.

Keep this module thin: the task backend serializes tasks by module path, so
``djangocms_automation.tasks.execute_action`` must remain stable. All
orchestration logic lives in :mod:`djangocms_automation.engine`.
"""

import datetime

from django.tasks import task

from . import engine


@task
def execute_action(action_id: int, data: dict | list | None = None, single_step: bool = False):
    """Execute a single AutomationAction by its ID."""
    engine.run_action(action_id, data=data, single_step=single_step)


def execute_pending_automations(timestamp: datetime.datetime | None = None) -> int:
    """Revive due pending/paused actions (cron entry point).

    Prefer the ``runautomations`` management command, which also fires due
    timer triggers.
    """
    return engine.revive_pending(timestamp)

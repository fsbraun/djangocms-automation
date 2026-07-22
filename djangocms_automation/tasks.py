import datetime
from django.db.models import Q
from django.utils.timezone import now
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.tasks import task
import traceback

from cms.models import CMSPlugin, Placeholder
from cms.utils.plugins import downcast_plugins, get_plugins_as_layered_tree

from .instances import FAILED, PENDING, RUNNING, WAITING, AutomationAction
from .models import AutomationContent
from .transitions import transition_action


def run_pending_automations(timestamp: datetime.datetime | None = None):
    """Execute all AutomationActions which are not waiting.
    Executes highly optimized db queries to get the AutomationActions and the
    assiociated plugins in bulk. Each action will have a `_plugin` attribute set
    to the corresponding plugin instance.
    The plugins are downcasted and have their `child_plugin_instances` set before the execute
    method is called."""

    if timestamp is None:
        timestamp = now()

    automation_actions = AutomationAction.objects.filter(
        Q(paused_until=None) | Q(paused_until__lte=timestamp),
        finished__isnull=True,
        automation_instance__testing=None,
    ).values_list("automation_instance__automation_content_id", flat=True)
    list(
        AutomationContent.objects.filter(pk__in=automation_actions, automation__is_active=True).select_related(
            "automation"
        )
    )
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
    action = transition_action(action_id, RUNNING, allowed_from=(PENDING, WAITING))
    if action is None:
        return
    plugin_dict = create_datastructure_for_automation(action)
    action._plugin = plugin_dict.get(action.plugin_ptr)
    plugin = action._plugin

    try:
        next_action = None
        status, output = plugin.execute(action, data=data, single_step=single_step, plugin_dict=plugin_dict)
    except Exception as e:
        tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        transition_action(
            action.pk,
            FAILED,
            allowed_from=(RUNNING,),
            result={"error": str(e), "traceback": tb_str},
            error=e,
        )
        return

    action = transition_action(action.pk, status, allowed_from=(RUNNING,), result=output)
    if action is None:
        return

    next_actions = plugin.get_next_actions(action)
    if status == RUNNING:
        transition_action(action.pk, WAITING, allowed_from=(RUNNING,))
    if next_actions and not single_step:
        for next_action in next_actions:
            execute_action.enqueue(next_action.pk, data=output)
    elif action.parent is not None and action.parent.finished is None:
        # Re-enqueue parent action if not finished
        execute_action.enqueue(action.parent.pk, data=data)


def execute_pending_automations(timestamp: datetime.datetime | None = None):
    """Task wrapper for run_pending_automations."""
    actions = AutomationAction.objects.filter(
        Q(paused_until=None) | Q(paused_until__lte=timestamp or now()),
        finished__isnull=True,
        state=PENDING,
        automation_instance__automation_content__automation__is_active=True,
    )
    for action in actions:
        execute_action.enqueue(action.pk)
    engine.run_action(action_id, data=data, single_step=single_step)


def execute_pending_automations(timestamp: datetime.datetime | None = None) -> int:
    """Revive due pending/paused actions (cron entry point).

    Prefer the ``runautomations`` management command, which also fires due
    timer triggers.
    """
    return engine.revive_pending(timestamp)

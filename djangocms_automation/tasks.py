import datetime
from django.db.models import Q
from django.utils.timezone import now
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.tasks import task
import traceback

from cms.models import CMSPlugin, Placeholder
from cms.utils.plugins import downcast_plugins, get_plugins_as_layered_tree

from .instances import AutomationAction, PENDING, RUNNING, COMPLETED, FAILED
from .models import AutomationContent


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


def _link_tree(plugins: list[CMSPlugin]):
    """
    Recursively links a list of CMSPlugin instances into a doubly-linked list.
    Each plugin in the list will have its `previous_plugin_instance` and `next_plugin_instance`
    attributes set to point to its adjacent plugins. The function also recursively applies
    the same linking to each plugin's child plugins.
    Args:
        plugins (list[CMSPlugin]): A list of CMSPlugin instances to link.
    Returns:
        None
    """
    previous_plugin = None
    for plugin in plugins:
        _link_tree(plugin.child_plugin_instances)
        if previous_plugin:
            previous_plugin.next_plugin_instance = plugin
        plugin.previous_plugin_instance = previous_plugin
        previous_plugin = plugin
    if previous_plugin is not None:
        previous_plugin.next_plugin_instance = None


def create_datastructure_for_automation(automation_action: AutomationAction) -> dict[int, AutomationContent]:
    placeholders = Placeholder.objects.filter(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_action.automation_instance.automation_content_id,
    )
    plugins = list(CMSPlugin.objects.filter(placeholder__in=placeholders, language=settings.LANGUAGE_CODE))
    plugins = list(downcast_plugins(plugins, placeholders, select_placeholder=True))
    root_plugins = get_plugins_as_layered_tree(plugins)
    _link_tree(root_plugins)
    return {plugin.uuid: plugin for plugin in plugins if hasattr(plugin, "uuid")}


@task
def execute_action(action_id: int, data: dict, single_step: bool = False):
    """Execute a single AutomationAction by its ID."""
    action = AutomationAction.objects.select_related("automation_instance").get(pk=action_id)
    action.state = RUNNING
    action.save(update_fields=["state"])
    plugin_dict = create_datastructure_for_automation(action)
    action._plugin = plugin_dict.get(action.plugin_ptr)

    try:
        next_action = None
        status, output = action._plugin.execute(action, data=data, single_step=single_step, plugin_dict=plugin_dict)
    except Exception as e:
        tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        action.state = FAILED
        action.result = {"error": str(e), "traceback": tb_str}
        action.finished = now()
        action.save()
        return

    action.state = status
    if output:
        action.result = output

    if status == COMPLETED:
        action.finished = now()
    action.save()

    next_actions = action._plugin.get_next_actions(action)
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

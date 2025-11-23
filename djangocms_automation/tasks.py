import datetime
from django.db import models, transaction
from django.db.models import Q
from django.utils.timezone import now
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.tasks import task

from cms.models import CMSPlugin, Placeholder
from cms.utils.plugins import downcast_plugins, get_plugins_as_layered_tree

from .models import AutomationAction, AutomationContent


@task
def run_pending_automations(timestamp: datetime.datetime | None = None):
    """Execute all AutomationActions which are not waiting.
    Executes highly optimized db queries to get the AutomationActions and the
    assiociated plugins in bulk. Each action will have a `_plugin` attribute set
    to the corresponding plugin instance.
    The plugins are downcasted and have their `child_plugin_instances` set before the execute
    method is called."""


    if timestamp is None:
        timestamp = now()

    subquery = AutomationContent.objects.filter(
        automation=models.OuterRef('automation_instance__automation')
    ).values('pk')[:1]

    automation_actions = list(
        AutomationAction.objects
        .filter(Q(paused_until=None) | Q(paused_until__lte=timestamp), finished__isnull=True, automation_instance__testing=None)
        .select_related('automation_instance', 'automation_instance__automation')
        .annotate(automation_content_id=models.Subquery(subquery, output_field=models.IntegerField()))
    )

    run_selected_actions(automation_actions)


def run_selected_actions(automation_actions, single_step: bool):
    placeholders = Placeholder.objects.filter(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id__in=[action.automation_content_id for action in automation_actions],
    )
    plugins = list(CMSPlugin.objects.filter(placeholder__in=placeholders, language=settings.LANGUAGE_CODE))
    plugins = list(downcast_plugins(plugins, placeholders, select_placeholder=True))
    get_plugins_as_layered_tree(list(plugins))
    plugin_dict = {
        plugin.uuid: plugin
        for plugin in plugins if hasattr(plugin, 'uuid')
    }

    for action in automation_actions:
        action._plugin = plugin_dict.get(action.status)
        if action._plugin:
            with transaction.atomic():
                action.refresh_from_db()  # Ensure we have the latest data
                # Did another worker change the action in the meantime?
                if action.finished is None and action.status == action._plugin.uuid:
                    action._plugin.execute(action, single_step=single_step)
                    action.save()

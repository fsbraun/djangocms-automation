from django.utils.translation import gettext as _

from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool


class AutomationPlugin(CMSPluginBase):
    module = _("Automation")
    render_template = "djangocms_automation/plugins/action.html"
    change_form_template = "djangocms_frontend/admin/base.html"

    def render(self, context, instance, placeholder):
        context.update(
            {
                "title": self.name,
                "instance": instance,
            }
        )
        return context


@plugin_pool.register_plugin
class AutomationTrigger(AutomationPlugin):
    name = _("Trigger")
    render_template = "djangocms_automation/plugins/trigger.html"


@plugin_pool.register_plugin
class AutomationIf(AutomationPlugin):
    name = _("Conditional")
    render_template = "djangocms_automation/plugins/if.html"
    allow_children = True
    child_classes = ["ThenPlugin", "ElsePlugin"]

    def render(self, context, instance, placeholder):
        context = super().render(context, instance, placeholder)
        empty = not instance.child_plugin_instances or not any(
            child.child_plugin_instances for child in instance.child_plugin_instances
        )
        context.update({"empty": empty})
        return context


@plugin_pool.register_plugin
class ThenPlugin(AutomationPlugin):
    name = _("Yes")
    render_template = "djangocms_automation/plugins/if_then.html"
    require_parent = True
    parent_classes = ["AutomationIf"]
    allow_children = True


@plugin_pool.register_plugin
class ElsePlugin(AutomationPlugin):
    name = _("No")
    render_template = "djangocms_automation/plugins/if_else.html"
    require_parent = True
    parent_classes = ["AutomationIf"]
    allow_children = True


@plugin_pool.register_plugin
class AutomationAction(AutomationPlugin):
    name = _("Example action")
    render_template = "djangocms_automation/plugins/action.html"

    allow_children = True
    child_classes = ["NextModifier"]


@plugin_pool.register_plugin
class NextModifier(AutomationPlugin):
    name = _("Trigger Automation")
    render_template = "djangocms_automation/modifiers/trigger.html"

    parent_classes = ["AutomationAction"]

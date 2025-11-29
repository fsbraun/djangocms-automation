from django.utils.translation import gettext as _

from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool

from . import forms, models
from .constants import Module

automation_plugins = []
action_plugins = []
modifier_plugins = []


def register_automation_plugin(cls):
    """Decorator to register an automation plugin with common settings."""
    plugin_pool.register_plugin(cls)
    automation_plugins.append(cls.__name__)
    if "Action" in cls.__name__:
        # Convention: Action plugins have "Action" in their class name
        action_plugins.append(cls.__name__)
    if "Modifier" in cls.__name__:
        # Convention: Modifier plugins have "Modifier" in their class name
        modifier_plugins.append(cls.__name__)
    return cls


class AutomationPlugin(CMSPluginBase):
    module = Module.CORE
    render_template = "djangocms_automation/plugins/action.html"
    change_form_template = "djangocms_frontend/admin/base.html"
    show_add_form = False
    allowed_models = ["djangocms_automation.AutomationContent"]

    def render(self, context, instance, placeholder):
        context.update(
            {
                "title": self.name,
                "instance": instance,
                "end": any(plugin.plugin_type == "EndModifier" for plugin in instance.child_plugin_instances or []),
            }
        )
        return context


@register_automation_plugin
class AutomationIf(AutomationPlugin):
    name = _("Conditional")
    module = Module.FLOW
    render_template = "djangocms_automation/plugins/if.html"

    show_add_form = True

    allow_children = True
    child_classes = ["ThenPlugin", "ElsePlugin"]

    model = models.ConditionalPluginModel
    form = forms.ConditionalPluginForm
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "question",
                    "condition",
                )
            },
        ),
        (_("Comment"), {"classes": ("collapse",), "fields": ("comment",)}),
    )

    def render(self, context, instance, placeholder):
        context = super().render(context, instance, placeholder)
        empty = not instance.child_plugin_instances or not any(
            child.child_plugin_instances for child in instance.child_plugin_instances
        )
        context.update({"empty": empty})
        return context


@register_automation_plugin
class ThenPlugin(AutomationPlugin):
    name = _("Yes")
    module = Module.FLOW

    render_template = "djangocms_automation/plugins/if_then.html"
    require_parent = True
    parent_classes = ["AutomationIf"]
    allow_children = True


@register_automation_plugin
class ElsePlugin(AutomationPlugin):
    name = _("No")
    module = Module.FLOW

    render_template = "djangocms_automation/plugins/if_else.html"
    require_parent = True
    parent_classes = ["AutomationIf"]
    allow_children = True


@register_automation_plugin
class AutomationSplit(AutomationPlugin):
    name = _("Split")
    module = Module.FLOW
    model = models.SplitPluginModel
    render_template = "djangocms_automation/plugins/split.html"

    show_add_form = False

    allow_children = True
    child_classes = ["AutomationPath"]


@register_automation_plugin
class AutomationPath(AutomationPlugin):
    name = _("Path")
    module = Module.FLOW
    render_template = "djangocms_automation/plugins/path.html"

    show_add_form = False
    require_parent = True
    allow_children = True
    parent_classes = ["AutomationSplit"]


@register_automation_plugin
class AutomationAction(AutomationPlugin):
    name = _("Example action")
    module = Module.ACTION

    model = models.BaseActionPluginModel

    render_template = "djangocms_automation/plugins/action.html"

    allow_children = True
    child_classes = modifier_plugins


class ModifierPlugin(AutomationPlugin):
    module = Module.MODIFIER
    render_template = "djangocms_automation/modifiers/general.html"
    parent_classes = action_plugins

    def render(self, context, instance, placeholder):
        context = super().render(context, instance, placeholder)
        context.update(
            {
                "icon": getattr(self, "icon", ""),
                "title": getattr(self, "name", ""),
                "class": getattr(self, "css_class", ""),
            }
        )
        return context


@register_automation_plugin
class NextModifier(ModifierPlugin):
    name = _("Trigger Automation")
    css_class = "next"
    icon = "bi-code-slash"


@register_automation_plugin
class EndModifier(ModifierPlugin):
    name = _("End")
    css_class = "end"
    icon = "bi-check2-square"


@register_automation_plugin
class OpenAIModifier(ModifierPlugin):
    name = _("OpenAI")
    css_class = "openai"
    icon = "bi-openai"


@register_automation_plugin
class DataModifier(ModifierPlugin):
    name = _("Data")
    css_class = "data"
    icon = "bi-database"
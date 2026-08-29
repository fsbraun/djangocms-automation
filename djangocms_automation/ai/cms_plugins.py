"""Plugin registration for the AI surface.

Lives here rather than in the core package's ``cms_plugins`` so that core has no
reason to import this package. django CMS discovers this module because ``ai``
is an installed app in its own right.
"""

from django.utils.translation import gettext_lazy as _

from ..cms_plugins import ActionPlugin, AutomationPlugin, action_plugins, register_automation_plugin
from ..constants import Module
from . import llm_action, models


@register_automation_plugin
class LLMAction(ActionPlugin):
    name = _("LLM Prompt")
    module = Module.AI
    icon = "bi-stars"

    model = llm_action.LLMActionPluginModel
    data_form = llm_action.LLMActionForm
    convert_data_form = False


@register_automation_plugin
class AutomationAgent(AutomationPlugin):
    name = _("Agent")
    module = Module.AI
    icon = "bi-stars"
    model = models.AgentPluginModel
    render_template = "djangocms_automation/plugins/agent.html"

    show_add_form = True
    allow_children = True
    child_classes = ["AutomationAgentTool"]

    fieldsets = (
        (None, {"fields": ("question", "model", "prompt", "system_prompt")}),
        (
            _("Limits"),
            {
                "classes": ("collapse",),
                "description": _(
                    "An agent chooses its own next step, so none of its cost is knowable in "
                    "advance. Reaching a limit fails the run rather than stopping quietly."
                ),
                "fields": ("max_turns", "max_tool_calls", "max_tokens", "deadline_seconds", "llm_timeout"),
            },
        ),
        (_("Comment"), {"classes": ("collapse",), "fields": ("comment",)}),
    )

    def render(self, context, instance, placeholder):
        context = super().render(context, instance, placeholder)
        context.update({"empty": not instance.child_plugin_instances})
        return context


@register_automation_plugin
class AutomationAgentTool(AutomationPlugin):
    name = _("Agent tool")
    module = Module.AI
    icon = "bi-plug"
    model = models.AgentToolPluginModel
    render_template = "djangocms_automation/plugins/agent_tool.html"

    show_add_form = True
    require_parent = True
    parent_classes = ["AutomationAgent"]
    allow_children = True
    child_classes = action_plugins

    fieldsets = (
        (None, {"fields": ("tool_name", "tool_description", "exposed_fields", "requires_approval")}),
        (_("Comment"), {"classes": ("collapse",), "fields": ("comment",)}),
    )

    def save_model(self, request, obj, form, change):
        """Default an irreversible action to needing approval.

        Only on first save, so an editor who deliberately turns it off is not
        overruled every time they touch the tool again.
        """
        if not change and obj.is_destructive():
            obj.requires_approval = True
        super().save_model(request, obj, form, change)

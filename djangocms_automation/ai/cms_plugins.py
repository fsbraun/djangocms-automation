"""Plugin registration for the AI surface.

Lives here rather than in the core package's ``cms_plugins`` so that core has no
reason to import this package. django CMS discovers this module because ``ai``
is an installed app in its own right.
"""

from django import forms
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


class AgentToolForm(forms.ModelForm):
    """Spells out what leaving the approval gate unset actually means.

    Django renders a nullable boolean as Unknown/Yes/No, and "Unknown" is a bad
    word for a security decision. The three states are: decide from what the
    action does, always ask, never ask.
    """

    requires_approval = forms.TypedChoiceField(
        label=_("Requires approval"),
        required=False,
        empty_value=None,
        coerce=lambda value: value == "true",
        choices=(
            ("", _("Automatic — ask before anything irreversible")),
            ("true", _("Always ask")),
            ("false", _("Never ask")),
        ),
        help_text=_("A person sees the call, and the arguments the model chose, before it runs."),
    )

    class Meta:
        model = models.AgentToolPluginModel
        fields = "__all__"


@register_automation_plugin
class AutomationAgentTool(AutomationPlugin):
    name = _("Agent tool")
    module = Module.AI
    icon = "bi-plug"
    model = models.AgentToolPluginModel
    form = AgentToolForm
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

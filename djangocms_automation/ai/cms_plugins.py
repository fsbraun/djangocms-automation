"""Plugin registration for the AI surface.

Lives here rather than in the core package's ``cms_plugins`` so that core has no
reason to import this package. django CMS discovers this module because ``ai``
is an installed app in its own right.
"""

from django.utils.translation import gettext_lazy as _

from ..cms_plugins import ActionPlugin, ai_step_plugins, register_automation_plugin
from ..constants import Module
from . import step


@register_automation_plugin
class AIStep(ActionPlugin):
    """Ask a model — and let it use the actions placed inside it."""

    name = _("Ask a Model")
    module = Module.AI
    icon = "bi-stars"

    model = step.AIStepPluginModel
    reports_to_model = "rows"  # the answer is the step's own product
    data_form = step.AIStepForm
    convert_data_form = False
    render_template = "djangocms_automation/plugins/ai_step.html"

    allow_children = True
    #: Left unset so the computed list below is what applies. See
    #: :meth:`get_child_class_overrides`.
    child_classes = None

    @classmethod
    def get_child_class_overrides(cls, slot, page=None, instance=None):
        """Actions — which inside this step are tools — and modifiers.

        Computed rather than declared, because every action is a possible tool
        and the list is whatever has registered by the time somebody opens the
        menu, minus anything that opted out with ``can_be_tool = False``.

        This hook rather than ``get_child_classes``: the latter also carries
        django CMS's caching protocol and an ``only_uncached`` pass, which an
        override would have to reimplement and keep in step. This one is asked a
        smaller question — *which names are allowed* — and the base does the
        rest.
        """
        from cms.plugin_pool import plugin_pool

        from ..cms_plugins import action_plugins, modifier_plugins

        configured = super().get_child_class_overrides(slot, page=page, instance=instance)
        if configured:
            # A project that pinned this plugin's children in CMS_PLACEHOLDER_CONF
            # meant it.
            return configured
        return list(modifier_plugins) + [
            name for name in action_plugins if getattr(plugin_pool.get_plugin(name), "can_be_tool", True)
        ]

    #: An AI step is an action, so it can be a tool of another AI step. The
    #: nesting limit rather than the plugin tree is what stops that running away.
    can_be_tool = True

    fieldsets = (
        (_("Intent"), {"fields": ("intent",)}),
        (None, {"fields": ("model", "prompt", "system_prompt")}),
        (_("Answer"), {"classes": ("collapse",), "fields": ("answer_format", "output_schema")}),
        (
            _("Limits"),
            {
                "classes": ("collapse",),
                "description": _(
                    "These bound a step that has tools. A step without tools makes one call, "
                    "and its cost is known in advance."
                ),
                "fields": ("max_turns", "max_tool_calls", "max_tokens", "deadline_seconds", "llm_timeout"),
            },
        ),
        (_("Comment"), {"classes": ("collapse",), "fields": ("comment",)}),
    )

    def render(self, context, instance, placeholder):
        context = super().render(context, instance, placeholder)
        children = instance.child_plugin_instances or []
        approval_tools = []
        for child in children:
            tool = child
            if not hasattr(tool, "needs_approval"):
                tool, _plugin = child.get_plugin_instance()
            if tool is not None and hasattr(tool, "needs_approval") and tool.needs_approval():
                approval_tools.append(tool)
        context.update({"tools": children, "approval_tools": approval_tools})
        return context


#: Registered so that an action can tell whether it is being edited as a tool
#: without core importing this package.
ai_step_plugins.append("AIStep")

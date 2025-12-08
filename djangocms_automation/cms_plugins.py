from django.utils.translation import gettext as _

from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool

from . import forms, models
from .constants import Module
from .utilities.expressions import validate_expression


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
    icon = None

    def render(self, context, instance, placeholder):
        context.update(
            {
                "title": self.name,
                "instance": instance,
                "icon": self.icon,
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
    data_form = None
    fieldsets = [
        (_("Comment"), {"classes": ("collapse",), "fields": ("comment",)}),
    ]

    def get_form(self, request, obj=None, **kwargs):
        """Use data_form if defined for additional data fields."""
        data_form_fields = self.get_data_form_fields(request, obj)
        data_form_fields["Media"] = type(
            "Media", (), {"js": (), "css": {"all": ("djangocms_automation/css/plugin_data_form.css",)}}
        )
        metaclass = type(self.form)
        new_form = metaclass(self.form.__name__, (self.form,), data_form_fields)
        kwargs["form"] = new_form
        return super().get_form(request, obj=obj, **kwargs)

    def get_data_form_fields(self, request, obj=None):
        """Return data_form if defined, else a basic form."""
        from django import forms

        if self.data_form:
            return {
                f_name: forms.CharField(
                    initial=f_name,
                    required=True,
                    validators=[validate_expression],
                    widget=forms.TextInput(attrs={"code": ""}),
                )
                for f_name in self.data_form.declared_fields.keys()
            }
        return {}

    def get_fieldsets(self, request, obj=None):
        """Return fieldsets including data_form fields if defined."""
        fieldsets = super().get_fieldsets(request, obj)
        if self.data_form:
            data_fields = list(self.data_form.declared_fields.keys())
            fieldsets = fieldsets + [
                (
                    _("Inputs"),
                    {
                        "fields": data_fields,
                        "classes": ("collapse",),
                        "description": _(
                            "<p>Each field is a data source for this action. Enter the value for the action either as a numeric or string literal "
                            "or as dotted path navigating the automation's data object.</p>"
                            "<p>Examples:</p>"
                            '<p><code>"info@django-cms.org"</code> (string literal)<br>'
                            "<code>42</code> (numeric literal)<br>"
                            "<code>user.email</code> (data path)</p>"
                        ),
                    },
                ),
            ]
        return fieldsets


@register_automation_plugin
class MailAction(AutomationAction):
    name = _("Send Email")
    module = Module.ACTION
    icon = "bi-envelope-at"

    model = models.BaseActionPluginModel
    data_form = forms.MailActionDataForm

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

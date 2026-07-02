from django import forms as django_forms
from django.utils.translation import gettext as _

from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool

from . import forms, models
from .actions import llm_action
from .actions import mail as actions_mail
from .actions import model_actions, user_input
from .constants import Module
from .utilities.expressions import validate_expression
from .utilities.templates import validate_template


automation_plugins = []
action_plugins = []
modifier_plugins = []


def register_automation_plugin(cls):
    """Decorator to register an automation plugin with common settings.

    Plugins are classified via their ``automation_category`` attribute
    (``"action"``, ``"modifier"``, or ``None`` for flow plugins). For
    backwards compatibility, plugins without the attribute fall back to the
    old name convention ("Action"/"Modifier" in the class name).
    """
    plugin_pool.register_plugin(cls)
    automation_plugins.append(cls.__name__)
    category = getattr(cls, "automation_category", ...)
    if category is ...:
        category = "action" if "Action" in cls.__name__ else "modifier" if "Modifier" in cls.__name__ else None
    if category == "action":
        action_plugins.append(cls.__name__)
    elif category == "modifier":
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
class ActionPlugin(AutomationPlugin):
    """Base CMS plugin for all action plugins.

    Renamed from ``AutomationAction`` (see migration 0007) to resolve the
    collision with the runtime model
    :class:`djangocms_automation.instances.AutomationAction`.

    Subclasses declare a ``data_form`` (a plain form whose declared fields
    define the action's inputs). Each field is rendered as an expression
    input — or a template textarea for ``Textarea`` widgets — and the
    entered values are persisted in the plugin model's ``config`` JSON
    field, from which the action resolves its inputs at runtime.
    """

    name = _("Example action")
    module = Module.ACTION
    automation_category = "action"

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

    # When False, the declared data_form fields are used as-is (choice
    # fields, JSON fields, ...) instead of being converted to expression /
    # template inputs.
    convert_data_form = True

    def get_data_form_fields(self, request, obj=None):
        """Build the dynamic config fields from the declared data_form.

        Values are seeded from the plugin's stored ``config``. Fields
        declared with a ``Textarea`` widget are treated as templates
        (``{{ path }}`` substitution); all others as expressions.
        """
        if not self.data_form:
            return {}
        config = (obj.config or {}) if obj is not None else {}
        if not self.convert_data_form:
            import copy

            fields = {}
            for f_name, declared in self.data_form.declared_fields.items():
                field = copy.deepcopy(declared)
                if f_name in config:
                    field.initial = config[f_name]
                fields[f_name] = field
            return fields
        fields = {}
        for f_name, declared in self.data_form.declared_fields.items():
            is_template = isinstance(declared.widget, django_forms.Textarea)
            fields[f_name] = django_forms.CharField(
                label=declared.label or f_name,
                help_text=declared.help_text,
                initial=config.get(f_name, f_name if not is_template else ""),
                required=declared.required,
                validators=[validate_template if is_template else validate_expression],
                widget=(
                    django_forms.Textarea(attrs={"rows": 4})
                    if is_template
                    else django_forms.TextInput(attrs={"code": ""})
                ),
            )
        return fields

    def save_model(self, request, obj, form, change):
        """Persist the dynamic data_form values into the config JSON field."""
        if self.data_form:
            obj.config = {
                f_name: form.cleaned_data.get(f_name, "")
                for f_name in self.data_form.declared_fields.keys()
                if f_name in form.cleaned_data
            }
        super().save_model(request, obj, form, change)

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
class MailAction(ActionPlugin):
    name = _("Send Email")
    module = Module.ACTION
    icon = "bi-envelope-at"

    model = actions_mail.MailActionPluginModel
    data_form = forms.MailActionDataForm

    render_template = "djangocms_automation/plugins/action.html"

    allow_children = True
    child_classes = modifier_plugins


@register_automation_plugin
class CreateModelAction(ActionPlugin):
    name = _("Create Record")
    module = Module.ACTION
    icon = "bi-database-add"

    model = model_actions.CreateModelActionModel
    data_form = model_actions.CreateModelActionForm
    convert_data_form = False


@register_automation_plugin
class UpdateModelAction(ActionPlugin):
    name = _("Update Records")
    module = Module.ACTION
    icon = "bi-database-gear"

    model = model_actions.UpdateModelActionModel
    data_form = model_actions.UpdateModelActionForm
    convert_data_form = False


@register_automation_plugin
class QueryModelAction(ActionPlugin):
    name = _("Query Records")
    module = Module.ACTION
    icon = "bi-database-down"

    model = model_actions.QueryModelActionModel
    data_form = model_actions.QueryModelActionForm
    convert_data_form = False


@register_automation_plugin
class LLMAction(ActionPlugin):
    name = _("LLM Prompt")
    module = Module.AI
    icon = "bi-stars"

    model = llm_action.LLMActionPluginModel
    data_form = llm_action.LLMActionForm
    convert_data_form = False


@register_automation_plugin
class UserInputAction(ActionPlugin):
    name = _("Wait for User")
    module = Module.HIL
    icon = "bi-person-check"

    model = user_input.UserInputActionPluginModel
    data_form = user_input.UserInputActionForm
    convert_data_form = False


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
class DataModifier(ModifierPlugin):
    name = _("Data")
    css_class = "data"
    icon = "bi-database"

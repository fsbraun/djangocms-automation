from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool
from django import forms as django_forms
from django.utils.translation import gettext as _

from . import forms, models
from .actions import mail as actions_mail
from .actions import model_actions, user_input
from .constants import Module
from .utilities.expressions import validate_expression
from .utilities.templates import validate_template

automation_plugins = []
action_plugins = []
modifier_plugins = []
#: Plugins that offer their child actions to a model. Registered by the AI app;
#: core needs the names only, so that an action can tell whether it is being
#: edited as a tool.
ai_step_plugins = []


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

    def __str__(self):
        """The plugin's name, resolved.

        ``CMSPluginBase.__str__`` returns ``self.name`` as it is, and a plugin
        name is a translation proxy — so Python raises ``__str__ returned
        non-string`` for anything that stringifies a plugin. The admin change
        form does: a fieldset with no name of its own falls back to rendering
        the plugin, and by default a plugin's first fieldset has no name.
        """
        return str(self.name)

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
        (_("Intent"), {"fields": ("intent",)}),
        (_("Decision"), {"fields": ("condition",)}),
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
class AutomationLoop(AutomationPlugin):
    name = _("Loop")
    module = Module.FLOW
    icon = "bi-arrow-repeat"
    model = models.LoopPluginModel
    form = forms.LoopPluginForm
    render_template = "djangocms_automation/plugins/loop.html"

    show_add_form = True

    # The body sits directly under the loop. A container child would only earn
    # its place if there were sibling branches to tell apart, as with a split's
    # paths or a conditional's yes/no; a loop has exactly one body.
    allow_children = True

    fieldsets = (
        (_("Intent"), {"fields": ("intent",)}),
        (
            _("Loop"),
            {
                "fields": (
                    "condition",
                    "max_iterations",
                )
            },
        ),
        (_("Comment"), {"classes": ("collapse",), "fields": ("comment",)}),
    )

    def render(self, context, instance, placeholder):
        context = super().render(context, instance, placeholder)
        # A loop's children *are* its body, unlike a conditional's, which are
        # branch containers that must themselves hold something.
        context.update({"empty": not instance.child_plugin_instances})
        return context


@register_automation_plugin
class AutomationSplit(AutomationPlugin):
    name = _("Split")
    module = Module.FLOW
    model = models.SplitPluginModel
    render_template = "djangocms_automation/plugins/split.html"

    show_add_form = True

    fieldsets = (
        (_("Intent"), {"fields": ("intent",)}),
        (_("Comment"), {"classes": ("collapse",), "fields": ("comment",)}),
    )

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

    #: Whether this action's effects can be taken back. Declared by the action's
    #: author, next to the code that does the damage, because nobody else knows.
    #: It sets the automatic approval gate when the action is used as a tool.
    destructive = False

    #: What a model is told this action returned, when it is used as a tool.
    #:
    #: ``"changes"`` — only what the action added to the rows it was handed.
    #: The default, and the safe one: most actions pass their input through and
    #: add a field, so reporting the rows would hand back whatever the
    #: automation happens to be carrying.
    #:
    #: ``"rows"`` — the rows themselves, for an action whose answer *is* data.
    #: A lookup has to say this, or it reports nothing worth having.
    #:
    #: Cardinality cannot tell the two apart — an action that filters its input
    #: returns fewer rows without having produced any, and a lookup can return
    #: exactly as many as it was given — so the action says which it is.
    reports_to_model = "changes"

    #: Whether an editor may offer this action to a model. True for everything
    #: by default: an action is a capability, and which capabilities an agent is
    #: given is the editor's decision rather than the author's. Set False for an
    #: action that is meaningless or unsafe when something other than a person
    #: chose to run it.
    can_be_tool = True

    #: Fields the wiring section adds when this action sits inside an AI step.
    tool_fields = ("tool_name", "tool_description", "requires_approval")
    fieldsets = [
        (_("Intent"), {"fields": ("intent",)}),
        (_("Comment"), {"classes": ("collapse",), "fields": ("comment",)}),
    ]

    def get_form(self, request, obj=None, **kwargs):
        """Use data_form if defined for additional data fields."""
        data_form_fields = self.get_data_form_fields(request, obj)
        data_form_fields["Media"] = type(
            "Media", (), {"js": (), "css": {"all": ("djangocms_automation/css/plugin_data_form.css",)}}
        )
        bases = (self.form,)
        if self.is_tool(request, obj) and self.data_form:
            # Wired inputs need a check that spans two fields, so the form gains
            # a base class rather than a function in its namespace.
            data_form_fields["tool_plugin"] = self
            bases = (WiredInputsMixin, self.form)
        metaclass = type(self.form)
        new_form = metaclass(self.form.__name__, bases, data_form_fields)
        kwargs["form"] = new_form
        return super().get_form(request, obj=obj, **kwargs)

    # When False, the declared data_form fields are used as-is (choice
    # fields, JSON fields, ...) instead of being converted to expression /
    # template inputs.
    convert_data_form = True

    #: Prefix of the companion switch rendered beside each input when this
    #: action is wired as a tool.
    MODEL_FILLS = "model_fills__"

    def is_tool(self, request, obj=None) -> bool:
        """Whether this action is being edited as a tool of an AI step.

        Asked of the parent, because that is what makes an action a tool. At
        add time the parent is in the query string; at change time it is on the
        object.
        """
        if not self.can_be_tool:
            return False
        parent = getattr(obj, "parent", None)
        if parent is None:
            parent_id = request.GET.get("plugin_parent")
            if not parent_id:
                return False
            from cms.models import CMSPlugin

            parent = CMSPlugin.objects.filter(pk=parent_id).first()
        return bool(parent and parent.plugin_type in ai_step_plugins)

    def get_render_template(self, context, instance, placeholder):
        """An action draws as a step in a flow, or as a tool inside an AI step.

        Same plugin, same instance — what differs is what it *is* in the place
        it sits. Routing the template here rather than rendering tool rows from
        the AI step's own template is what keeps each tool a rendered plugin,
        so django CMS wraps it and an editor can double-click it open like
        anything else.
        """
        if instance is not None and instance.parent_id and instance.parent.plugin_type in ai_step_plugins:
            return "djangocms_automation/plugins/tool.html"
        return self.render_template

    def get_data_form_fields(self, request, obj=None):
        """Build the dynamic config fields from the declared data_form.

        Values are seeded from the plugin's stored ``config``. Fields
        declared with a ``Textarea`` widget are treated as templates
        (``{{ path }}`` substitution); all others as expressions.

        Inside an AI step each input also gets a switch saying whether the
        model fills it. Flipping it on is what stops the input asking for a
        value, because it would never use one — the switch and the requirement
        are the same decision, and splitting them is what produces a form that
        demands an expression in order to discard it.
        """
        if not self.data_form:
            return {}
        config = (obj.config or {}) if obj is not None else {}
        wired = self.is_tool(request, obj)
        exposed = set(getattr(obj, "exposed_fields", None) or []) if obj is not None else set()
        if not self.convert_data_form:
            import copy

            fields = {}
            for f_name, declared in self.data_form.base_fields.items():
                field = copy.deepcopy(declared)
                if f_name in config:
                    field.initial = config[f_name]
                if wired:
                    # As below: whether a value is needed depends on the switch
                    # beside it, and the form decides that when it cleans.
                    field.required = False
                fields[f_name] = field
                self._add_wiring_switch(fields, f_name, wired, exposed)
            return fields
        fields = {}
        for f_name, declared in self.data_form.base_fields.items():
            is_template = isinstance(declared.widget, django_forms.Textarea)
            fields[f_name] = django_forms.CharField(
                label=declared.label or f_name,
                help_text=declared.help_text,
                initial=config.get(f_name, f_name if not is_template else ""),
                # Not required while wired: whether a value is needed depends on
                # the switch beside it, which is not known until the form is
                # cleaned. The form checks it there instead.
                required=declared.required and not wired,
                validators=[validate_template if is_template else validate_expression],
                widget=(
                    django_forms.Textarea(attrs={"rows": 4})
                    if is_template
                    else django_forms.TextInput(attrs={"code": ""})
                ),
            )
            self._add_wiring_switch(fields, f_name, wired, exposed)
        return fields

    def _pair_placed_fields(self, fieldsets):
        """Put each already-placed input beside its switch, in situ.

        A plugin that arranges its own fields keeps that arrangement; only the
        entries naming one of its inputs become a two-item row.
        """
        inputs = set(self.data_form.base_fields)

        def paired_entry(entry):
            # A fieldset entry is a field name or a tuple of them rendered on
            # one line. Both can name inputs, and a switch left out of either
            # is a control the admin never draws.
            if isinstance(entry, (list, tuple)):
                names = []
                for name in entry:
                    names.append(name)
                    if name in inputs:
                        names.append(self.MODEL_FILLS + name)
                return tuple(names)
            if entry in inputs:
                return (entry, self.MODEL_FILLS + entry)
            return entry

        return [
            (label, {**options, "fields": [paired_entry(entry) for entry in options.get("fields") or ()]})
            for label, options in fieldsets
        ]

    def _add_wiring_switch(self, fields, name, wired, exposed):
        """The companion switch for one input.

        Added for literal-valued actions as well as expression-valued ones:
        ``get_fieldsets`` pairs *every* declared input with its switch, so an
        action that skipped them asked the admin for fields that did not exist
        and raised ``FieldError`` before its form could render.
        """
        if not wired:
            return
        fields[self.MODEL_FILLS + name] = django_forms.BooleanField(
            label=_("The model decides"),
            required=False,
            initial=name in exposed,
            help_text=_("Leave off to bind this input to an expression the model never sees."),
        )

    def save_model(self, request, obj, form, change):
        """Persist the dynamic data_form values into the config JSON field.

        An input the model fills has no configured value, so it is left out of
        the config entirely rather than stored empty — the action would
        otherwise try to resolve an empty expression on any run where the model
        sent nothing.
        """
        exposed = []
        if self.data_form:
            if self.is_tool(request, obj):
                exposed = [
                    f_name for f_name in self.data_form.base_fields if form.cleaned_data.get(self.MODEL_FILLS + f_name)
                ]
                obj.exposed_fields = exposed
            obj.config = {
                f_name: form.cleaned_data.get(f_name, "")
                for f_name in self.data_form.base_fields
                if f_name in form.cleaned_data and f_name not in exposed
            }
        super().save_model(request, obj, form, change)

    def get_fieldsets(self, request, obj=None):
        """Return fieldsets including data_form fields if defined."""
        fieldsets = super().get_fieldsets(request, obj)
        wired = self.is_tool(request, obj)
        if wired:
            tool_fieldset = (
                _("As a tool"),
                {
                    "fields": list(self.tool_fields),
                    "description": _(
                        "This action sits inside an AI step, so a model may call it. What it "
                        "is called and when to use it are the only things the model knows "
                        "about it."
                    ),
                },
            )
            # Intent is the first decision for every step. Tool wiring is a
            # property of this use of the action, so it follows that name.
            fieldsets = list(fieldsets)
            fieldsets[1:1] = [tool_fieldset]
        if self.data_form:
            # Only the inputs the plugin has not placed itself. An action that
            # declares no layout gets all of them here, which is the usual case;
            # one that arranges its own — the AI step groups its budgets away
            # from its prompt — would otherwise have every field twice.
            placed = _named_fields(fieldsets)
            if wired:
                # An input the plugin placed itself still needs its switch, and
                # the switch has to be *in a fieldset* or the admin never
                # renders it — the field would exist on the form and be
                # invisible, so nothing could be exposed to the model.
                fieldsets = self._pair_placed_fields(fieldsets)
            data_fields = [name for name in self.data_form.base_fields if name not in placed]
            if not data_fields:
                return fieldsets
            if wired:
                # Each input beside its own switch, so the decision is made
                # where the input is.
                data_fields = [(name, self.MODEL_FILLS + name) for name in data_fields]
            fieldsets = list(fieldsets)
            # Intent stays first; configuration follows the tool wiring when
            # present and otherwise follows the intent directly. The optional
            # comment remains last.
            fieldsets.insert(
                2 if wired else 1,
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
            )
        return fieldsets


@register_automation_plugin
class MailAction(ActionPlugin):
    name = _("Send Email")
    module = Module.ACTION
    destructive = True
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
    destructive = True
    icon = "bi-database-add"

    model = model_actions.CreateModelActionModel
    data_form = model_actions.CreateModelActionForm
    convert_data_form = False


@register_automation_plugin
class UpdateModelAction(ActionPlugin):
    name = _("Update Records")
    module = Module.ACTION
    destructive = True
    icon = "bi-database-gear"

    model = model_actions.UpdateModelActionModel
    data_form = model_actions.UpdateModelActionForm
    convert_data_form = False


@register_automation_plugin
class QueryModelAction(ActionPlugin):
    name = _("Query Records")
    reports_to_model = "rows"  # the records found are the answer
    module = Module.ACTION
    icon = "bi-database-down"

    model = model_actions.QueryModelActionModel
    data_form = model_actions.QueryModelActionForm
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


def _named_fields(fieldsets) -> set:
    """Every field name a fieldset places, flattening same-line rows."""
    names = set()
    for _label, options in fieldsets or []:
        for entry in options.get("fields") or ():
            names.update(entry if isinstance(entry, (list, tuple)) else [entry])
    return names


class WiredInputsMixin:
    """Requires a value for every input the model is *not* filling.

    ``required`` cannot answer this on its own: whether an input needs a value
    depends on the switch beside it, which is not known when the field is
    built. So the check moves to the form, where both are available.

    A real class rather than a function stuffed into the generated form's
    namespace, because ``super()`` has to work. Django's admin subclasses this
    form again, so ``super(type(self), self)`` would start its lookup below a
    class that is no longer the one this method was defined on — and find this
    method a second time, forever.
    """

    #: The plugin whose ``data_form`` this checks. Set on the generated class.
    tool_plugin = None

    def clean(self):
        cleaned = super().clean()
        plugin = self.tool_plugin
        for name, declared in plugin.data_form.base_fields.items():
            if not declared.required:
                continue
            if cleaned.get(plugin.MODEL_FILLS + name):
                continue
            # Asked of the field itself, because "is this filled in" is a
            # question only it can answer. ``0`` satisfies an IntegerField and
            # an unticked box does not satisfy a required BooleanField, and no
            # test of emptiness written here gets both right.
            try:
                declared.validate(cleaned.get(name))
            except django_forms.ValidationError:
                self.add_error(name, _("Enter a value, or let the model decide."))
        return cleaned

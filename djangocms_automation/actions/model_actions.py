"""Django model CRUD actions: create, update, and query model instances.

For safety, actions may only touch models explicitly allowed via the
``AUTOMATION_ALLOWED_MODELS`` setting (a list of ``"app_label.Model"``
labels; default: none).
"""

from __future__ import annotations

from django import forms
from django.apps import apps as django_apps
from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from ..models import BaseActionPluginModel
from ..tools import EXPRESSION_SYNTAX_CHECK
from ..utilities.expressions import ExpressionError, Literal, resolve_expression, validate_expression
from ..utilities.json import model_to_row

MAX_QUERY_LIMIT = 1000


def get_allowed_model_labels() -> list[str]:
    """Get the model labels automations may interact with."""
    return list(getattr(settings, "AUTOMATION_ALLOWED_MODELS", []))


def get_allowed_model(label: str | None):
    """Resolve an allowed model label to the model class.

    :raises ValueError: If the label is missing, unknown, or not allowed.
    """
    if not label:
        raise ValueError("No model configured for this action.")
    if label not in get_allowed_model_labels():
        raise ValueError(
            f"Model '{label}' is not allowed for automations. Add it to the AUTOMATION_ALLOWED_MODELS setting."
        )
    try:
        return django_apps.get_model(label)
    except (LookupError, ValueError) as exc:
        raise ValueError(f"Model '{label}' cannot be resolved: {exc}") from exc


def _model_choices():
    return [(label, label) for label in get_allowed_model_labels()]


def _validate_expression_mapping(value):
    """Validate a ``{name: expression}`` JSON mapping."""
    if value in (None, ""):
        return
    if not isinstance(value, dict):
        raise forms.ValidationError(_("Enter a JSON object mapping field names to expressions."))
    for key, expr in value.items():
        try:
            validate_expression(str(expr))
        except ExpressionError as exc:
            raise forms.ValidationError(_("Invalid expression for '%(key)s': %(error)s") % {"key": key, "error": exc})


# The one question a model cannot be asked: it supplies values, not paths into
# the automation's data. Marked so that offering one of these fields to a model
# sets this check aside and keeps every other one the action declared.
setattr(_validate_expression_mapping, EXPRESSION_SYNTAX_CHECK, True)


def _resolve_mapping(mapping: dict, context: dict) -> dict:
    # A Literal is passed through rather than stringified: it is already the
    # value, and ``str()`` would lose the marker that says so.
    return {
        name: resolve_expression(expr if isinstance(expr, Literal) else str(expr), context)
        for name, expr in (mapping or {}).items()
    }


def _validate_model_fields(model, names, *, lookups: bool = False) -> None:
    """Check mapping keys refer to existing model fields."""
    for name in names:
        field_name = name.split("__", 1)[0] if lookups else name
        try:
            model._meta.get_field(field_name)
        except Exception as exc:  # FieldDoesNotExist
            raise ValueError(f"Unknown field '{field_name}' on {model._meta.label}: {exc}") from exc


class ModelActionBaseForm(forms.Form):
    """Shared config fields for model actions."""

    model = forms.ChoiceField(
        label=_("Model"),
        choices=_model_choices,
        help_text=_("Only models listed in AUTOMATION_ALLOWED_MODELS are available."),
    )


class CreateModelActionForm(ModelActionBaseForm):
    field_mapping = forms.JSONField(
        label=_("Field mapping"),
        validators=[_validate_expression_mapping],
        help_text=_('JSON object mapping model fields to expressions, e.g. {"email": "user.email", "active": "1"}.'),
    )


class UpdateModelActionForm(ModelActionBaseForm):
    filters = forms.JSONField(
        label=_("Filters"),
        validators=[_validate_expression_mapping],
        help_text=_('JSON object mapping lookups to expressions, e.g. {"email": "user.email"}.'),
    )
    field_mapping = forms.JSONField(
        label=_("Field mapping"),
        validators=[_validate_expression_mapping],
        help_text=_("JSON object mapping model fields to expressions with the new values."),
    )


class QueryModelActionForm(ModelActionBaseForm):
    filters = forms.JSONField(
        label=_("Filters"),
        required=False,
        validators=[_validate_expression_mapping],
        help_text=_("JSON object mapping lookups to expressions. Empty matches all rows (up to the limit)."),
    )
    fields = forms.CharField(
        label=_("Fields"),
        required=False,
        help_text=_("Comma-separated field names to include in the returned records. Empty includes all fields."),
    )
    order_by = forms.CharField(label=_("Order by"), required=False)
    limit = forms.IntegerField(
        label=_("Limit"),
        required=False,
        min_value=1,
        max_value=MAX_QUERY_LIMIT,
        initial=100,
    )


class CreateModelActionModel(BaseActionPluginModel):
    """Create one model instance per data row."""

    #: Config keys holding a mapping whose *values* are expressions rather than
    #: values. What an editor writes there is a path into the automation's data;
    #: what a model supplies is the value itself. See
    #: :class:`~djangocms_automation.utilities.expressions.Literal`.
    expression_mappings = frozenset({"field_mapping"})
    default_outputs = {"id": {"field": "created_id"}}
    literal_fields = frozenset({"model"})

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, context, inputs) -> dict:
        model = get_allowed_model(inputs.get("model"))
        values = inputs.get("field_mapping") or {}
        _validate_model_fields(model, values)
        with transaction.atomic():
            obj = model.objects.create(**values)
        return {"id": obj.pk}


class UpdateModelActionModel(BaseActionPluginModel):
    """Update model instances matching per-row filters."""

    #: Config keys holding a mapping whose *values* are expressions rather than
    #: values. What an editor writes there is a path into the automation's data;
    #: what a model supplies is the value itself. See
    #: :class:`~djangocms_automation.utilities.expressions.Literal`.
    expression_mappings = frozenset({"filters", "field_mapping"})
    default_outputs = {"count": {"field": "updated_count"}}
    literal_fields = frozenset({"model"})

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, context, inputs) -> dict:
        model = get_allowed_model(inputs.get("model"))
        filters = inputs.get("filters") or {}
        values = inputs.get("field_mapping") or {}
        if not filters:
            raise ValueError("Refusing to update without filters.")
        _validate_model_fields(model, filters, lookups=True)
        _validate_model_fields(model, values)
        with transaction.atomic():
            count = model.objects.filter(**filters).update(**values)
        return {"count": count}


class QueryModelActionModel(BaseActionPluginModel):
    """Query model instances and return them as the records list result."""

    #: Config keys holding a mapping whose *values* are expressions rather than
    #: values. What an editor writes there is a path into the automation's data;
    #: what a model supplies is the value itself. See
    #: :class:`~djangocms_automation.utilities.expressions.Literal`.
    expression_mappings = frozenset({"filters"})
    default_outputs = {"records": {"field": "records"}}
    literal_fields = frozenset({"model", "fields", "order_by", "limit"})

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, context, inputs) -> dict:
        model = get_allowed_model(inputs.get("model"))
        filters = inputs.get("filters") or {}
        _validate_model_fields(model, filters, lookups=True)
        queryset = model.objects.filter(**filters)
        order_by = (inputs.get("order_by") or "").strip()
        if order_by:
            queryset = queryset.order_by(*[part.strip() for part in order_by.split(",") if part.strip()])
        limit = min(int(inputs.get("limit") or 100), MAX_QUERY_LIMIT)
        field_names = [part.strip() for part in (inputs.get("fields") or "").split(",") if part.strip()]
        return {"records": [model_to_row(obj, field_names or None) for obj in queryset[:limit]]}

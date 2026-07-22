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
from ..utilities.expressions import ExpressionError, resolve_expression, validate_expression
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


def _resolve_mapping(mapping: dict, context: dict) -> dict:
    return {name: resolve_expression(str(expr), context) for name, expr in (mapping or {}).items()}


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
        help_text=_("Comma-separated field names to include in the output rows. Empty includes all fields."),
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

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows: list) -> list:
        model = get_allowed_model((self.config or {}).get("model"))
        mapping = (self.config or {}).get("field_mapping") or {}
        _validate_model_fields(model, mapping.keys())
        rows = rows or [{}]
        output = []
        with transaction.atomic():
            for row in rows:
                row = row if isinstance(row, dict) else {"value": row}
                context = {**row, "data": rows}
                values = _resolve_mapping(mapping, context)
                obj = model.objects.create(**values)
                output.append({**row, "_created_id": obj.pk})
        return output


class UpdateModelActionModel(BaseActionPluginModel):
    """Update model instances matching per-row filters."""

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows: list) -> list:
        model = get_allowed_model((self.config or {}).get("model"))
        filters = (self.config or {}).get("filters") or {}
        mapping = (self.config or {}).get("field_mapping") or {}
        if not filters:
            raise ValueError("Refusing to update without filters (would affect every row).")
        _validate_model_fields(model, filters.keys(), lookups=True)
        _validate_model_fields(model, mapping.keys())
        rows = rows or [{}]
        output = []
        with transaction.atomic():
            for row in rows:
                row = row if isinstance(row, dict) else {"value": row}
                context = {**row, "data": rows}
                count = model.objects.filter(**_resolve_mapping(filters, context)).update(
                    **_resolve_mapping(mapping, context)
                )
                output.append({**row, "_updated": count})
        return output


class QueryModelActionModel(BaseActionPluginModel):
    """Query model instances and emit them as data rows (per run)."""

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows: list) -> list:
        config = self.config or {}
        model = get_allowed_model(config.get("model"))
        filters = config.get("filters") or {}
        _validate_model_fields(model, filters.keys(), lookups=True)

        first_row = rows[0] if rows and isinstance(rows[0], dict) else {}
        context = {**first_row, "data": rows or []}
        queryset = model.objects.all()
        if filters:
            queryset = queryset.filter(**_resolve_mapping(filters, context))
        order_by = (config.get("order_by") or "").strip()
        if order_by:
            queryset = queryset.order_by(*[part.strip() for part in order_by.split(",") if part.strip()])
        limit = min(int(config.get("limit") or 100), MAX_QUERY_LIMIT)
        field_names = [part.strip() for part in (config.get("fields") or "").split(",") if part.strip()]
        return [model_to_row(obj, field_names or None) for obj in queryset[:limit]]

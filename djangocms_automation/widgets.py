from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING

from django import forms
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from .triggers import Trigger


def _trigger_registry():
    """Import lazily so trigger declarations may themselves use our widgets."""
    from .triggers import trigger_registry

    return trigger_registry


def _get_trigger(trigger_id: str) -> Trigger | None:
    return _trigger_registry().get(trigger_id)


class TriggerSelectWidget(forms.Select):
    """Select widget for triggers that shows description + schema.

    Enhancements over a plain Select:
    - Prominent description display below the select.
    - Collapsible <details> block for JSON schema inspection.
    - Uses external JS file to update description and schema without reload.
    """

    template_name = ""  # not using Django's template-based rendering here

    class Media:
        js = ("djangocms_automation/js/trigger_select.js",)

    def __init__(self, attrs=None, choices: Iterable = ()):  # choices ignored, taken from registry
        super().__init__(attrs, choices=self._build_choices())

    def _build_choices(self):
        return _trigger_registry().get_choices()

    def render(self, name, value, attrs=None, renderer=None):
        if value is None:
            value = ""
        attrs = self.build_attrs(self.attrs, attrs)

        # Add data attribute with registry for JS
        registry_json = self._js_registry_json()
        attrs["data-trigger-registry"] = registry_json

        select_html_parts = [f'<select name="{escape(name)}"']
        for k, v in attrs.items():
            select_html_parts.append(f' {escape(k)}="{escape(v)}"')
        select_html_parts.append(' id="trigger-select"')
        select_html_parts.append(">")

        # Build option tags (no title tooltips - description shown below)
        for trigger_id, label in self._build_choices():
            selected = " selected" if str(value) == str(trigger_id) else ""
            select_html_parts.append(f'<option value="{escape(trigger_id)}"{selected}>{escape(label)}</option>')
        select_html_parts.append("</select>")

        # Current trigger description and schema
        current_trigger = _get_trigger(str(value)) or (
            _get_trigger(self._build_choices()[0][0]) if self._build_choices() else None
        )
        if current_trigger:
            schema_json = json.dumps(current_trigger.data_schema, indent=2, ensure_ascii=False)
            desc_html = escape(current_trigger.description)
        else:
            schema_json = "{}"
            desc_html = _("No trigger selected.")

        info_block = f'<div id="trigger-description" style="margin-top:0.75rem; padding:0.5rem; background:#var(--hairline-color); border-left:3px solid #0066cc; font-size:14px;">{desc_html}</div>'
        if schema_json != "{}":
            info_block += (
                '<details id="trigger-schema-details" style="margin-top:0.5rem;">'
                f'<summary style="cursor:pointer; font-weight:500;">{_("Schema details for input data")}</summary>'
                f'<pre id="trigger-schema" style="margin-top:0.5rem; background:#f8f9fa; border:1px solid #ddd; padding:0.5rem; max-height:24rem; overflow:auto; font-size:12px;">{escape(schema_json)}</pre>'
                "</details>"
            )

        html = "".join(select_html_parts) + info_block
        return mark_safe(html)

    def _js_registry_json(self) -> str:
        mapping = {}
        for trigger_id, _label in self._build_choices():
            trig = _get_trigger(trigger_id)
            if trig:
                schema_str = json.dumps(trig.data_schema, indent=2, ensure_ascii=False)
                mapping[trigger_id] = {
                    "description": str(trig.description),
                    "schema": schema_str,
                }
        return json.dumps(mapping, ensure_ascii=False)


class SchemaWidget(forms.Textarea):
    """Progressively enhanced editor for a deliberately small JSON Schema subset.

    The textarea remains the only successful form control. JavaScript turns it
    into a table for flat object schemas and keeps the JSON value synchronized;
    without JavaScript it stays a normal textarea, preserving the field's
    existing behaviour and validation path.
    """

    #: The choices offered for a field's type. Two of them are not JSON Schema
    #: types at all — ``email`` is a ``format`` on a string and ``string_array``
    #: an array of them — because what an editor picks is a kind of value,
    #: while what is emitted stays canonical.
    schema_types = (
        ("string", _("Text")),
        ("email", _("Email address")),
        ("number", _("Number")),
        ("integer", _("Whole number")),
        ("boolean", _("Yes / no")),
        ("string_array", _("List of text")),
    )

    class Media:
        js = ("djangocms_automation/js/schema_widget.js",)
        css = {"all": ("djangocms_automation/css/schema_widget.css",)}

    def __init__(self, attrs=None):
        defaults = {"rows": 12, "class": "schema-widget-source"}
        if attrs:
            supplied = dict(attrs)
            if supplied.get("class"):
                defaults["class"] = f"{defaults['class']} {supplied.pop('class')}"
            defaults.update(supplied)
        super().__init__(defaults)

    def format_value(self, value):
        # ``"null"`` as well as ``None``: a ``JSONField`` with nothing in it
        # serialises to the JSON literal, and "nothing configured" has to reach
        # the editor as an empty table rather than as a schema it cannot read.
        if value is None or (isinstance(value, str) and value.strip() in ("", "null")):
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, indent=2, ensure_ascii=False)

    def render(self, name, value, attrs=None, renderer=None):
        value_json = self.format_value(value)
        textarea_attrs = self.build_attrs(self.attrs, attrs)
        textarea_attrs["name"] = name
        textarea = f"<textarea{forms.utils.flatatt(textarea_attrs)}>{escape(value_json)}</textarea>"

        source_id = textarea_attrs.get("id", name)
        container_attrs = {
            "id": f"{source_id}_builder",
            "class": "schema-widget",
            "data-schema-types": json.dumps(
                [{"value": key, "label": str(label)} for key, label in self.schema_types],
                ensure_ascii=False,
            ),
            "data-field-label": str(_("Field")),
            "data-type-label": str(_("Type")),
            "data-required-label": str(_("Required")),
            "data-description-label": str(_("Description")),
            "data-choices-label": str(_("Choices")),
            "data-choices-help": str(_("One choice per line")),
            "data-add-label": str(_("Add a field")),
            "data-remove-label": str(_("Remove field")),
            "data-name-required": str(_("Enter a field name.")),
            "data-name-duplicate": str(_("Field names must be unique.")),
            "data-json-label": str(_("Edit as JSON")),
            "data-builder-label": str(_("Use field editor")),
            "data-consequence": str(_("Anything not listed here is refused.")),
            "data-unsupported": str(
                _(
                    "This schema uses features the editor cannot show — nesting, oneOf, or a pattern. "
                    "Edit it as JSON, or simplify it."
                )
            ),
        }
        container = f"<div{forms.utils.flatatt(container_attrs)}></div>"
        return mark_safe(textarea + container)


class ConditionBuilderWidget(forms.Widget):
    """Widget for building JSON-based conditions with field/operator/value triplets.

    Renders as:
    - Multiple condition rows with field name, operator dropdown, and value input
    - AND/OR logic selector
    - Add/remove condition buttons

    Stores data as JSON in format:
    {
        "logic": "and|or",
        "conditions": [
            {"field": "status", "operator": "==", "value": "active"},
            {"field": "count", "operator": ">", "value": "5"}
        ]
    }
    """

    template_name = ""  # Custom rendering
    operators = [
        ("==", _("equals")),
        ("!=", _("not equals")),
        ("<", _("less than")),
        (">", _("greater than")),
        ("<=", _("less than or equal")),
        (">=", _("greater than or equal")),
        ("contains", _("contains")),
        ("not_contains", _("does not contain")),
        ("starts_with", _("starts with")),
        ("ends_with", _("ends with")),
        ("in", _("in list")),
        ("not_in", _("not in list")),
    ]

    class Media:
        js = ("djangocms_automation/js/condition_builder.js",)
        css = {"all": ("djangocms_automation/css/condition_builder.css",)}

    def render(self, name, value, attrs=None, renderer=None):
        if value and isinstance(value, str):
            # Already JSON string
            value_json = value
        elif value:
            # Convert dict to JSON
            value_json = json.dumps(value, ensure_ascii=False)
        else:
            value_json = ""

        # Hidden input to store JSON value
        hidden_attrs = self.build_attrs(attrs or {}, {"type": "hidden", "name": name})
        if value_json:
            hidden_attrs["value"] = value_json

        hidden_input = f"<input{forms.utils.flatatt(hidden_attrs)}>"

        # Container for the builder UI (initialized by JS)
        container_id = hidden_attrs.get("id", name) + "_builder"
        operators = json.dumps([(key, str(value)) for key, value in self.operators], ensure_ascii=False)
        dataset = (
            f'data-operators="{escape(operators)}" '
            f'data-and-label="{escape(str(_("All of the following (AND)")))}" '
            f'data-or-label="{escape(str(_("Any of the following (OR)")))}"'
            f'data-add-label="{escape(str(_("Add condition")))}"'
        )
        container = f'<div id="{escape(container_id)}" class="condition-builder-widget" {dataset}></div>'

        return mark_safe(hidden_input + container)

    def value_from_datadict(self, data, files, name):
        """Extract JSON value from form data and filter out empty conditions."""
        value = data.get(name, "")
        if value:
            try:
                # Parse and validate JSON
                parsed = json.loads(value)

                # Filter out conditions with empty field names
                if isinstance(parsed, dict) and "conditions" in parsed:
                    parsed["conditions"] = [
                        cond for cond in parsed.get("conditions", []) if cond.get("field", "").strip()
                    ]
                    # Return empty string if no conditions remain
                    if not parsed["conditions"]:
                        return ""
                    # Return cleaned JSON
                    return json.dumps(parsed, ensure_ascii=False)

                return value
            except json.JSONDecodeError:
                return ""
        return ""

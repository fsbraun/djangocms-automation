"""Field selection shared by action inputs and declared output destinations."""

import json
import re

from django import forms
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext as _


def _readable_path(path):
    """Turn a stable machine path into compact editor-facing text."""
    special = {
        "loop.entry": _("Current list entry"),
        "loop.index": _("Current list index"),
    }
    if path in special:
        return str(special[path])
    parts = []
    for segment in str(path).split("."):
        if segment.isdigit() and parts:
            parts[-1] += f"[{segment}]"
        else:
            parts.append(segment.replace("_", " ").capitalize())
    return " › ".join(parts)


def automation_io_summary(content, triggers):
    """Describe the automation's public boundary for its title block."""
    declared = content.data_fields or {}

    def label(name, schema=None):
        metadata = declared.get(name) or {}
        return str(metadata.get("label") or (schema or {}).get("title") or _readable_path(name))

    inputs = {}
    accepts_other_fields = False
    for trigger in triggers:
        schema = trigger.data_schema or {}
        for name, field_schema in (schema.get("properties") or {}).items():
            inputs.setdefault(name, label(name, field_schema))
        if schema.get("additionalProperties", True) is not False:
            accepts_other_fields = True

    input_labels = [inputs[name] for name in sorted(inputs)]
    if accepts_other_fields:
        input_labels.append(str(_("Other fields accepted")))
    if not input_labels:
        input_labels.append(str(_("No declared fields")))

    if content.output_fields:
        output_labels = [label(name) for name in content.output_fields]
    else:
        output_labels = [str(_("Complete final item"))]

    return {
        "uses": ", ".join(input_labels),
        "produces": ", ".join(output_labels),
    }


def data_summary(plugin):
    """Describe configured sources, never execute bindings while rendering."""
    from cms.plugin_pool import plugin_pool

    uses = set()
    templates = plugin._template_fields() if hasattr(plugin, "_template_fields") else set()
    literal = getattr(plugin, "literal_fields", ())
    convert = getattr(plugin_pool.get_plugin(plugin.plugin_type), "convert_data_form", True)

    from .utilities.templates import referenced_paths

    for name, value in (getattr(plugin, "config", {}) or {}).items():
        if name in literal:
            continue
        if isinstance(value, dict) and "field" in value:
            uses.add(value["field"] + ("." + value["path"] if value.get("path") else ""))
        elif name in getattr(plugin, "expression_mappings", ()):
            for source in (value or {}).values():
                uses.update(referenced_paths(source))
        elif name in templates or convert:
            uses.update(referenced_paths(value))
    outputs = getattr(plugin, "outputs", {}) or getattr(plugin, "default_outputs", {})
    produces = [
        f"{name} → {binding.get('field', '?')}" + (" (append)" if binding.get("mode") == "append" else "")
        for name, binding in outputs.items()
        if binding
    ]
    return {"uses": ", ".join(sorted(uses)), "produces": ", ".join(produces)}


def schema_fields(schema, prefix=""):
    """Yield declared paths only. ``[]`` denotes list entries, not a binding."""
    schema = schema if isinstance(schema, dict) else {}
    if prefix:
        yield prefix, schema
    for key, spec in schema.get("properties", {}).items():
        yield from schema_fields(spec, f"{prefix}.{key}" if prefix else key)
    kinds = schema.get("type", [])
    if (kinds == "array" or isinstance(kinds, list) and "array" in kinds) and isinstance(schema.get("items"), dict):
        yield from schema_fields(schema["items"], f"{prefix}[]")


def schema_type(schema):
    kind = schema.get("type", "object" if "properties" in schema else "")
    labels = {
        "string": _("Text"),
        "integer": _("Whole number"),
        "number": _("Number"),
        "boolean": _("Yes / no"),
        "object": _("Object"),
        "array": _("List"),
        "null": _("Null"),
    }
    if isinstance(kind, list):
        return " / ".join(labels.get(value, value) for value in kind)
    label = labels.get(kind, _("Unknown / dynamic"))
    if kind == "object" and schema.get("additionalProperties", True) is not False:
        label += _(" (additional fields possible)")
    return label


def result_fields(plugin):
    """The action's result contract, before destination mapping."""
    return [
        {"name": name, "type": schema_type(spec)}
        for result, schema in plugin.get_result_schemas().items()
        for name, spec in schema_fields(schema, result)
    ]


def automation_fields(*, trigger=None, placeholder=None, exclude_plugin=None):
    """Catalogue possible fields for one path, without consulting run tables.

    Keep separate declarations for a shared name: different producers can
    have different types. This is a catalogue, not an input validation schema.
    """
    if trigger is not None:
        placeholder = trigger.placeholder() if trigger.pk else None
        content = trigger.automation_content if trigger.automation_content_id else None
        triggers = [trigger]
    else:
        content = placeholder.source if placeholder else None
        triggers = []
    rows = []

    def add(schema, prefix, source, availability, producer=None):
        for name, spec in schema_fields(schema, prefix):
            rows.append(
                {
                    "name": name,
                    "type": schema_type(spec),
                    "source": source,
                    "availability": availability,
                    "producer": producer,
                }
            )

    from cms.utils.plugins import downcast_plugins

    from .models import AutomationTrigger, BaseActionPluginModel

    if trigger is None and content and placeholder:
        triggers = AutomationTrigger.objects.filter(automation_content=content, slot=placeholder.slot)
    for entry in triggers:
        schema = entry.data_schema or {}
        add(schema, "", _("Trigger"), _("Starting field (if supplied)"))
        if schema.get("additionalProperties", True) is not False:
            add({}, "*", _("Trigger"), _("Additional starting fields may be supplied"))
    if placeholder:
        for step in downcast_plugins(list(placeholder.get_plugins()), [placeholder]):
            if not isinstance(step, BaseActionPluginModel) or step.pk == exclude_plugin:
                continue
            schemas = step.get_result_schemas()
            for result, binding in (step.outputs or step.default_outputs).items():
                key = (binding or {}).get("field")
                if key:
                    schema = schemas.get(result, {})
                    if binding.get("mode") == "append":
                        schema = {"type": "array", "items": schema}
                    add(
                        schema, key, step.intent or step.plugin_type, _("Produced later; may be absent"), step.position
                    )
    # Saving an action registers its destination globally on the content.
    # Use those optional constraints only for this path's actual field names,
    # otherwise another trigger's saved destinations leak into its catalogue.
    roots = {row["name"].split(".", 1)[0].split("[", 1)[0] for row in rows}
    for key, spec in (getattr(content, "data_fields", {}) or {}).items():
        if key in roots and spec.get("schema"):
            add(spec["schema"], key, _("Destination schema"), _("Declared, not initialized"))
    return rows


def available_fields(plugin):
    """The same catalogue as the trigger, restricted to selectable data paths."""
    choices = {}
    if not plugin or not plugin.placeholder_id:
        return choices
    for row in automation_fields(placeholder=plugin.placeholder, exclude_plugin=plugin.pk):
        key = row["name"]
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.\w+)*", key):
            continue
        availability = row["availability"]
        if row["producer"] is not None:
            availability = (
                _("Later step; may be absent")
                if row["producer"] >= plugin.position
                else _("Earlier step; may be absent")
            )
        label = f"{key} — {row['type']} — {row['source']} — {availability}"
        choices[key] = f"{choices[key]}; {label}" if key in choices else label
    choices.update({"loop.entry": "Current list entry", "loop.index": "Current list index"})
    return choices


class DataInputWidget(forms.TextInput):
    def __init__(self, choices=(), *, template=False, attrs=None):
        super().__init__(attrs)
        self.choices = choices
        self.template = template

    class Media:
        js = ("djangocms_automation/js/data_picker.js",)

    def render(self, name, value, attrs=None, renderer=None):
        widget = forms.Textarea(attrs={"rows": 4}) if self.template else forms.TextInput()
        options = format_html_join("", '<option value="{}">{}</option>', self.choices)
        return format_html(
            '<div class="automation-data-input" data-template="{}"><label>Use data from '
            '<select aria-label="Use data from"><option value="">Choose a field…</option>{}</select></label>{}</div>',
            "true" if self.template else "false",
            options,
            widget.render(name, value, attrs, renderer),
        )


class OutputWidget(forms.Textarea):
    def __init__(self, results=(), choices=(), attrs=None):
        super().__init__(attrs)
        self.results = tuple(results)
        self.choices = choices

    class Media:
        js = ("djangocms_automation/js/data_picker.js",)

    def render(self, name, value, attrs=None, renderer=None):
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return super().render(name, value, attrs, renderer)
        else:
            parsed = value or {}
        if not isinstance(parsed, dict):
            return super().render(name, value, attrs, renderer)
        if not self.results and not parsed:
            return forms.HiddenInput().render(name, value, attrs, renderer)
        rows = []
        for result in dict.fromkeys((*self.results, *parsed)):
            binding = parsed.get(result) or {}
            rows.append(
                format_html(
                    '<div data-result="{}"><label>{} — Save result in '
                    '<input data-destination value="{}" placeholder="New or existing field" list="{}_fields"></label>'
                    '<select data-mode aria-label="How to save the result">'
                    '<option value="replace" {}>Replace value</option><option value="append" {}>Append to list</option></select></div>',
                    result,
                    result,
                    binding.get("field", ""),
                    name,
                    "selected" if binding.get("mode", "replace") == "replace" else "",
                    "selected" if binding.get("mode") == "append" else "",
                )
            )
        options = format_html_join("", '<option value="{}">{}</option>', self.choices)
        return format_html(
            '<div class="automation-output-picker">{}<datalist id="{}_fields">{}</datalist>'
            "<details><summary>Output mappings as JSON</summary>{}</details></div>",
            format_html_join("", "{}", ((row,) for row in rows)),
            name,
            options,
            super().render(name, value, attrs, renderer),
        )


def validate_outputs(value):
    if not isinstance(value, dict):
        raise forms.ValidationError("Output mappings must be a JSON object.")
    targets = set()
    for name, binding in value.items():
        if binding is None:
            continue
        if not isinstance(binding, dict):
            raise forms.ValidationError(f"Invalid destination for {name}.")
        key = binding.get("field", "")
        if not isinstance(key, str) or not key.isidentifier() or key == "loop":
            raise forms.ValidationError("Use a stable field key containing letters, numbers, and underscores.")
        if key in targets:
            raise forms.ValidationError(f"Two outputs write the same field: {key}.")
        targets.add(key)
        if binding.get("mode", "replace") not in ("replace", "append"):
            raise forms.ValidationError("Choose Replace value or Append to list.")

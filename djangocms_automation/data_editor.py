"""Field selection shared by action inputs and declared output destinations."""

import json
import re

from django import forms
from django.utils.html import format_html, format_html_join


def data_summary(plugin):
    """Describe configured sources, never execute bindings while rendering."""
    from cms.plugin_pool import plugin_pool

    uses = set()
    templates = plugin._template_fields() if hasattr(plugin, "_template_fields") else set()
    literal = getattr(plugin, "literal_fields", ())
    convert = getattr(plugin_pool.get_plugin(plugin.plugin_type), "convert_data_form", True)

    def expression(value):
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_]\w*(?:\.\w+)*", value):
            uses.add(value)

    for name, value in (getattr(plugin, "config", {}) or {}).items():
        if name in literal:
            continue
        if isinstance(value, dict) and "field" in value:
            uses.add(value["field"] + ("." + value["path"] if value.get("path") else ""))
        elif name in getattr(plugin, "expression_mappings", ()):
            for source in (value or {}).values():
                expression(source)
        elif name in templates:
            uses.update(re.findall(r"{{\s*([\w.]+)", str(value)))
        elif convert:
            expression(value)
    outputs = getattr(plugin, "outputs", {}) or getattr(plugin, "default_outputs", {})
    produces = [
        f"{name} → {binding.get('field', '?')}" + (" (append)" if binding.get("mode") == "append" else "")
        for name, binding in outputs.items()
        if binding
    ]
    return {"uses": ", ".join(sorted(uses)), "produces": ", ".join(produces)}


def available_fields(plugin):
    """Browse declared fields without running a workflow or reading live payloads."""
    choices = {}
    if not plugin or not plugin.placeholder_id:
        return choices
    content = plugin.placeholder.source
    for key, spec in (getattr(content, "data_fields", {}) or {}).items():
        choices[key] = spec.get("label", key)

    def walk(properties, prefix=""):
        for key, spec in properties.items():
            path = f"{prefix}.{key}" if prefix else key
            choices[path] = spec.get("title", path)
            if spec.get("type") == "object":
                walk(spec.get("properties", {}), path)

    from cms.utils.plugins import downcast_plugins

    from .models import AutomationTrigger, BaseActionPluginModel

    if content:
        for trigger in AutomationTrigger.objects.filter(automation_content=content, slot=plugin.placeholder.slot):
            walk((trigger.data_schema or {}).get("properties", {}))
    for step in downcast_plugins(list(plugin.placeholder.get_plugins()), [plugin.placeholder]):
        if isinstance(step, BaseActionPluginModel) and step.pk != plugin.pk:
            for binding in (step.outputs or step.default_outputs).values():
                key = (binding or {}).get("field")
                if key:
                    choices.setdefault(key, f"{key} — {step.intent or step.plugin_type}")
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

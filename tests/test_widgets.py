"""Tests for the custom structured-value widgets."""

import json

from djangocms_automation.triggers import trigger_registry
from djangocms_automation.widgets import ConditionBuilderWidget, SchemaWidget, TriggerSelectWidget


def test_trigger_select_widget_renders_choices_and_description():
    widget = TriggerSelectWidget()
    html = widget.render("type", "timer")
    # Should contain select, options, description, and schema details
    assert '<select name="type"' in html
    for trigger_id, label in trigger_registry.get_choices():
        assert f'<option value="{trigger_id}"' in html
        assert str(label) in html
    # Should show description and schema for selected trigger
    assert "Timer" in html or "Mail" in html
    assert "Schema details for input data" in html
    # Should have JS registry data attribute
    assert "data-trigger-registry" in html
    # Should be safe HTML
    assert isinstance(html, str)


def test_trigger_select_widget_registry_json():
    widget = TriggerSelectWidget()
    js_data = widget._js_registry_json()
    mapping = json.loads(js_data)
    # Should contain all registered triggers
    for trigger_id, _ in trigger_registry.get_choices():
        assert trigger_id in mapping
        assert "description" in mapping[trigger_id]
        assert "schema" in mapping[trigger_id]


def test_condition_builder_widget_renders_and_extracts():
    widget = ConditionBuilderWidget()
    value = {
        "logic": "and",
        "conditions": [
            {"field": "status", "operator": "==", "value": "active"},
            {"field": "count", "operator": ">", "value": "5"},
        ],
    }
    html = widget.render("condition", value)
    # Should contain hidden input and builder container
    assert 'type="hidden"' in html
    assert "condition-builder-widget" in html
    # Should serialize value as JSON
    assert "status" in html and "count" in html
    # Should have operator data attributes
    assert "data-operators" in html
    # Extraction: value_from_datadict returns cleaned JSON
    data = {"condition": json.dumps(value)}
    extracted = widget.value_from_datadict(data, None, "condition")
    parsed = json.loads(extracted)
    assert parsed["logic"] == "and"
    assert len(parsed["conditions"]) == 2
    # If all fields are empty, returns empty string
    empty_data = {
        "condition": json.dumps({"logic": "and", "conditions": [{"field": "", "operator": "==", "value": ""}]})
    }
    assert widget.value_from_datadict(empty_data, None, "condition") == ""


def test_schema_widget_renders_dict_as_the_single_fallback_textarea():
    widget = SchemaWidget()
    schema = {
        "type": "object",
        "properties": {"email": {"type": "string", "description": "Who wrote in"}},
        "required": ["email"],
        "additionalProperties": False,
    }

    html = widget.render("data_schema", schema, attrs={"id": "id_data_schema"})

    assert html.count('name="data_schema"') == 1, "only one value is posted"
    assert '<textarea class="schema-widget-source"' in html
    assert 'id="id_data_schema_builder"' in html
    assert "Who wrote in" in html
    assert "Anything not listed here is refused." in html


def test_schema_widget_preserves_raw_json_for_the_escape_hatch():
    widget = SchemaWidget()
    raw = '{\n  "oneOf": [{"type": "string"}, {"type": "number"}]\n}'

    html = widget.render("schema", raw)

    assert "&quot;oneOf&quot;" in html
    assert "This schema uses features the editor cannot show" in html
    assert widget.value_from_datadict({"schema": raw}, {}, "schema") == raw


def test_schema_widget_declares_its_assets():
    media = SchemaWidget().media

    assert "djangocms_automation/js/schema_widget.js" in media._js
    assert "djangocms_automation/css/schema_widget.css" in media._css["all"]


def test_both_schema_fields_use_the_same_json_widget(settings):
    from django import forms

    from djangocms_automation.ai.step import AIStepForm
    from djangocms_automation.triggers import CodeTrigger

    settings.AUTOMATION_LLM_MODELS = ["anthropic/claude-opus-4-8"]
    assert isinstance(CodeTrigger.base_fields["data_schema"], forms.JSONField)
    assert isinstance(CodeTrigger.base_fields["data_schema"].widget, SchemaWidget)
    assert isinstance(AIStepForm.base_fields["output_schema"], forms.JSONField)
    assert isinstance(AIStepForm.base_fields["output_schema"].widget, SchemaWidget)
    assert "schema_widget.js" in str(CodeTrigger().media)
    assert "schema_widget.js" in str(AIStepForm().media)

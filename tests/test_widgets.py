"""Tests for TriggerSelectWidget and ConditionBuilderWidget rendering and value extraction."""

import json

from djangocms_automation.widgets import TriggerSelectWidget, ConditionBuilderWidget
from djangocms_automation.triggers import trigger_registry


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

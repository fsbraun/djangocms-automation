"""Tests for the custom structured-value widgets."""

import json

import pytest

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


def test_an_unconfigured_schema_reaches_the_editor_empty():
    """``JSONField`` renders "nothing" as the JSON literal ``null``.

    Which is not an object, so the editor read it as a schema it could not show
    and dropped to raw JSON with a warning — on every field nobody had
    configured yet, which is to say on every field the first time it is opened.
    """
    widget = SchemaWidget()

    assert widget.format_value(None) == ""
    assert widget.format_value("") == ""
    assert widget.format_value("null") == "", "a JSONField with nothing in it"
    assert widget.format_value("  null  ") == ""

    rendered = widget.render("data_schema", "null")
    assert "<textarea" in rendered and ">null</textarea>" not in rendered


@pytest.mark.django_db
def test_the_admin_page_carries_the_widget(admin_client, admin_user):
    """What the browser is actually sent: the script, the styles, the textarea
    it enhances, and the container it draws into."""
    from django.urls import reverse

    from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger

    automation = Automation.objects.create(name="Widget", is_active=True)
    content = AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Widget")
    trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="code", position=0)

    url = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])
    html = admin_client.get(url).content.decode()

    assert "schema_widget.js" in html and "schema_widget.css" in html
    assert 'class="schema-widget-source"' in html
    assert 'class="schema-widget"' in html
    # Adjacent, because the script finds its textarea as the previous sibling.
    assert 'class="schema-widget"' in html.split("</textarea>")[1][:200]


def test_the_mode_toggle_sits_in_the_same_place_in_both_modes():
    """The control that switches how the field is edited must not move.

    A button that is bottom-right of the table and top-left of the textarea is
    two controls to learn rather than one.
    """
    from pathlib import Path

    from django.conf import settings as django_settings  # noqa: F401

    js = (
        Path(__file__).resolve().parent.parent / "djangocms_automation/static/djangocms_automation/js/schema_widget.js"
    ).read_text()

    # Both renderers wrap their toggle in the same footer element.
    assert js.count("footer.className = 'schema-widget__footer';") == 2
    assert js.count("'button schema-widget__toggle'") == 2

    css = (
        Path(__file__).resolve().parent.parent
        / "djangocms_automation/static/djangocms_automation/css/schema_widget.css"
    ).read_text()

    assert "justify-content: flex-end;" in css, "inline-end in both modes"
    assert ".schema-widget__toggle {" in css and "border-radius" in css, "and looks like a button"


def test_email_is_offered_as_a_kind_of_value():
    """Not a JSON type — a ``format`` on a string, the way ``string_array`` is
    an array of them. What an editor picks is a kind of value; what is emitted
    stays canonical."""
    offered = dict(SchemaWidget.schema_types)

    assert "email" in offered
    assert "string" in offered, "and plain text is still there"

    js = _widget_js()
    assert "format: 'email'" in js, "composed as a format"
    assert "definition.format !== 'email'" in js, "and only that one is understood"


def _widget_js():
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "djangocms_automation/static/djangocms_automation/js/schema_widget.js"
    ).read_text()


def test_a_format_beside_choices_goes_to_the_json_editor():
    """An email row carries no choices, so recomposing one would drop the enum.

    The widget's one hard rule is that it never round-trips a schema into a
    lossy one, so it declines to show the table instead.
    """
    assert "Object.hasOwn(definition, 'enum')" in _widget_js()

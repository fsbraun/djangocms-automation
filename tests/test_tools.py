"""The tool contract (phase 1.2).

The theme running through these: the schema is what the model is *told*, and the
validation is what the model is *held to*. They are derived from the same Django
form so they cannot drift, but only one of them is a boundary.
"""

import pytest
from django import forms

from djangocms_automation.ai.llm import ToolCall
from djangocms_automation.ai.tools import (
    ToolResult,
    ToolSpec,
    ToolValidationError,
    schema_from_form,
    validate_arguments,
)


class SampleForm(forms.Form):
    """Stands in for an action's ``data_form``, covering the field types in use."""

    subject = forms.CharField(label="Subject", max_length=120, help_text="Shown in the email header.")
    body = forms.CharField(widget=forms.Textarea, required=False)
    recipient = forms.EmailField(label="Recipient")
    copies = forms.IntegerField(required=False, min_value=1, max_value=5)
    urgency = forms.ChoiceField(choices=[("low", "Low"), ("high", "High")], required=False)
    tags = forms.MultipleChoiceField(choices=[("a", "A"), ("b", "B")], required=False)
    payload = forms.JSONField(required=False)
    active = forms.BooleanField(required=False)
    due = forms.DateField(required=False)


# --------------------------------------------------------------------------
# Deriving the schema from the form
# --------------------------------------------------------------------------


def test_field_types_map_onto_json_schema():
    """An action author writes no schema: the form already declares one."""
    schema = schema_from_form(SampleForm)
    properties = schema["properties"]

    assert properties["subject"]["type"] == "string"
    assert properties["subject"]["maxLength"] == 120
    # Django gives EmailField a default max_length, and carrying it through is
    # correct: the model is told the real limit.
    assert properties["recipient"] == {
        "type": "string",
        "format": "email",
        "maxLength": 320,
        "description": "Recipient",
    }
    assert properties["copies"] == {"type": "integer", "minimum": 1, "maximum": 5}
    assert properties["payload"]["type"] == "object"
    assert properties["active"]["type"] == "boolean"
    assert properties["due"] == {"type": "string", "format": "date"}


def test_choices_become_an_enum():
    """Telling the model the permitted values is the cheapest way to stop it inventing one."""
    properties = schema_from_form(SampleForm)["properties"]

    assert properties["urgency"] == {"type": "string", "enum": ["low", "high"]}
    assert properties["tags"] == {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}


def test_label_and_help_text_become_the_description():
    properties = schema_from_form(SampleForm)["properties"]
    assert properties["subject"]["description"] == "Subject Shown in the email header."


def test_required_fields_are_marked_required():
    schema = schema_from_form(SampleForm)
    assert set(schema["required"]) == {"subject", "recipient"}


def test_the_schema_forbids_extra_properties():
    """Strict tool calling requires it, and it is what stops a model smuggling
    in a field the editor never offered."""
    assert schema_from_form(SampleForm)["additionalProperties"] is False


def test_include_limits_the_schema_to_the_exposed_fields():
    """Everything omitted is bound by the editor and never shown to the model."""
    schema = schema_from_form(SampleForm, include=["subject", "body"])

    assert set(schema["properties"]) == {"subject", "body"}
    assert schema["required"] == ["subject"]
    assert "recipient" not in schema["properties"], "a bound field must not leak into the schema"


# --------------------------------------------------------------------------
# Holding the model to it
# --------------------------------------------------------------------------


def test_valid_arguments_are_coerced_by_the_form():
    """A model sends strings; the form turns them into the types the action wants."""
    cleaned = validate_arguments(
        SampleForm,
        {"subject": "Hi", "recipient": "a@example.com", "copies": "3"},
        allowed=["subject", "recipient", "copies"],
    )
    assert cleaned["copies"] == 3
    assert cleaned["recipient"] == "a@example.com"


def test_an_unexposed_argument_is_refused():
    """The schema is a hint; this is the boundary.

    A model can ask for anything, so a field the tool did not offer must be
    rejected here even though the schema never mentioned it.
    """
    with pytest.raises(ToolValidationError, match="Unknown argument"):
        validate_arguments(
            SampleForm,
            {"subject": "Hi", "recipient": "evil@example.com"},
            allowed=["subject"],
        )


def test_an_invalid_value_is_reported_with_its_field():
    with pytest.raises(ToolValidationError, match="recipient"):
        validate_arguments(
            SampleForm, {"subject": "Hi", "recipient": "not-an-email"}, allowed=["subject", "recipient"]
        )


def test_a_missing_required_argument_is_reported():
    with pytest.raises(ToolValidationError, match="subject"):
        validate_arguments(SampleForm, {}, allowed=["subject"])


def test_fields_the_tool_did_not_expose_are_not_required_of_the_model():
    """``recipient`` is required on the form but bound by the editor, so a model
    that never sees it must not be failed for omitting it."""
    cleaned = validate_arguments(SampleForm, {"subject": "Hi"}, allowed=["subject"])
    assert cleaned == {"subject": "Hi"}


def test_arguments_must_be_an_object():
    with pytest.raises(ToolValidationError, match="must be an object"):
        validate_arguments(SampleForm, ["subject"], allowed=["subject"])


def test_empty_arguments_from_a_malformed_reply_fail_cleanly():
    """The LLM layer turns unparseable arguments into ``{}``; this is where that lands."""
    with pytest.raises(ToolValidationError):
        validate_arguments(SampleForm, {}, allowed=["subject", "recipient"])


# --------------------------------------------------------------------------
# ToolSpec
# --------------------------------------------------------------------------


def test_a_spec_renders_the_wire_format():
    spec = ToolSpec(name="send_email", description="Send one email.", parameters=schema_from_form(SampleForm))
    wire = spec.to_wire()

    assert wire["type"] == "function"
    assert wire["function"]["name"] == "send_email"
    assert wire["function"]["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize("name", ["", "has space", "has.dot", "x" * 65, "emoji🙂"])
def test_an_unusable_tool_name_is_refused(name):
    """The rule is the intersection of every provider's, so a name that works
    on one provider and not another never reaches production."""
    with pytest.raises(ValueError, match="not usable"):
        ToolSpec(name=name, description="ok", parameters={})


def test_a_tool_without_a_description_is_refused():
    """The description is the only thing telling the model when to use the tool."""
    with pytest.raises(ValueError, match="needs a description"):
        ToolSpec(name="send_email", description="   ", parameters={})


# --------------------------------------------------------------------------
# ToolResult
# --------------------------------------------------------------------------


def test_a_long_result_is_truncated_visibly():
    """A tool returning a thousand rows would otherwise spend the whole context
    window on one observation."""
    result = ToolResult(call_id="c1", content="x" * 100).truncate(20)

    assert result.content.startswith("x" * 20)
    assert "truncated" in result.content
    assert "80 more characters" in result.content


def test_a_short_result_is_returned_unchanged():
    result = ToolResult(call_id="c1", content="short", rows=[{"a": 1}])
    assert result.truncate(100) is result


def test_truncation_keeps_the_rows_that_flow_onward():
    """What the model sees is capped; what the automation carries is not."""
    result = ToolResult(call_id="c1", content="y" * 50, rows=[{"a": 1}]).truncate(10)
    assert result.rows == [{"a": 1}]


def test_a_tool_call_is_what_the_model_asked_for():
    call = ToolCall(id="c1", name="send_email", arguments={"subject": "Hi"})
    assert (call.id, call.name, call.arguments) == ("c1", "send_email", {"subject": "Hi"})


# --------------------------------------------------------------------------
# Real actions, unmodified
# --------------------------------------------------------------------------


def test_a_shipped_action_becomes_a_tool_with_no_changes_to_it():
    """The acceptance criterion for this phase.

    ``MailActionDataForm`` was written long before any of this and knows nothing
    about tools. If deriving from ``data_form`` works, it is already callable.
    """
    from djangocms_automation.forms import MailActionDataForm

    schema = schema_from_form(MailActionDataForm)

    assert schema["properties"]["subject"]["type"] == "string"
    assert schema["properties"]["recipient_email"]["format"] == "email"
    assert set(schema["required"]) == {"subject", "body", "recipient_email"}
    assert schema["additionalProperties"] is False


def test_binding_the_recipient_keeps_it_out_of_the_models_reach():
    """The shape a careful editor actually wants.

    The model may write the message; it may not choose who receives it.
    """
    from djangocms_automation.forms import MailActionDataForm

    exposed = ["subject", "body"]
    schema = schema_from_form(MailActionDataForm, include=exposed)
    assert "recipient_email" not in schema["properties"]

    with pytest.raises(ToolValidationError, match="Unknown argument"):
        validate_arguments(
            MailActionDataForm,
            {"subject": "Hi", "body": "…", "recipient_email": "attacker@example.com"},
            allowed=exposed,
        )


@pytest.mark.django_db
def test_overrides_bypass_expression_resolution(settings):
    """An editor's input is an expression; a model's is already the value.

    Without this the literal ``"Hello"`` would be read as a data path and
    resolve to nothing.
    """
    from djangocms_automation.actions.mail import MailActionPluginModel

    # A bare model instance has no registered plugin_type, so every input is
    # treated as an expression here; "static" is the literal form.
    action = MailActionPluginModel(config={"subject": "row_subject", "body": '"static"'})
    rows = [{"row_subject": "From the data"}]

    resolved = action.resolve_inputs(rows[0], rows)
    assert resolved["subject"] == "From the data", "an editor's expression still resolves"

    overridden = action.resolve_inputs(rows[0], rows, overrides={"subject": "Hello"})
    assert overridden["subject"] == "Hello", "a model's literal is used as it stands"
    assert overridden["body"] == "static", "untouched inputs still resolve normally"

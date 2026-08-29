"""Tool calling in the provider-independent LLM wrapper (phase 1.1).

Everything here runs against a stubbed provider. No test may reach a network or
need credentials — an agent's failure paths are mostly things a live provider
will not do on demand, so they have to be scripted.
"""

import types
from unittest import mock

import pytest

from djangocms_automation.ai import llm
from djangocms_automation.models import APIKey


class FakeRateLimitError(Exception):
    pass


class FakeAPIConnectionError(Exception):
    pass


def make_fake_litellm(completion, supports_tools=True):
    fake = types.SimpleNamespace()
    fake.completion = completion
    fake.RateLimitError = FakeRateLimitError
    fake.APIConnectionError = FakeAPIConnectionError
    fake.supports_function_calling = lambda model: supports_tools
    return fake


def make_tool_call(name, arguments, call_id="call_1"):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


def make_response(content=None, tool_calls=None, finish_reason="stop", model="anthropic/claude-opus-4-8"):
    message = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message, finish_reason=finish_reason)],
        model=model,
        usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up the weather.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}


@pytest.fixture
def llm_settings(settings):
    settings.AUTOMATION_LLM_MODELS = ["anthropic/claude-opus-4-8"]
    return settings


@pytest.fixture
def api_key(db):
    return APIKey.objects.create(name="Anthropic", service="anthropic", api_key="sk-test", is_active=True)


# --------------------------------------------------------------------------
# Requesting tools
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_tool_request_is_normalized(llm_settings, api_key):
    """Whatever shape the provider uses, the caller sees ToolCall objects."""
    response = make_response(
        tool_calls=[make_tool_call("get_weather", '{"city": "Berlin"}')],
        finish_reason="tool_calls",
    )

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(lambda **kw: response)):
        result = llm.complete(model="anthropic/claude-opus-4-8", prompt="weather?", tools=[WEATHER_TOOL])

    assert result.wants_tools
    assert result.finish_reason == "tool_calls"
    assert [(call.name, call.arguments) for call in result.tool_calls] == [("get_weather", {"city": "Berlin"})]
    assert result.tool_calls[0].id == "call_1"


@pytest.mark.django_db
def test_tools_and_tool_choice_reach_the_provider(llm_settings, api_key):
    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        return make_response(content="done")

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        llm.complete(
            model="anthropic/claude-opus-4-8",
            prompt="hi",
            tools=[WEATHER_TOOL],
            tool_choice="auto",
            timeout=12,
        )

    assert seen["tools"] == [WEATHER_TOOL]
    assert seen["tool_choice"] == "auto"
    assert seen["timeout"] == 12


@pytest.mark.django_db
def test_no_tools_means_no_tool_parameters(llm_settings, api_key):
    """A plain completion must not start sending tool parameters."""
    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        return make_response(content="hello")

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        result = llm.complete(model="anthropic/claude-opus-4-8", prompt="hi")

    assert "tools" not in seen and "tool_choice" not in seen and "timeout" not in seen
    assert result.wants_tools is False
    assert result.text == "hello"


@pytest.mark.django_db
def test_a_model_without_tool_support_is_a_configuration_error(llm_settings, api_key):
    """Dropping the tools and answering anyway is the dangerous alternative.

    An agent whose tools were silently ignored produces confident prose instead
    of doing the work, which is much harder to diagnose than an error.
    """
    fake = make_fake_litellm(lambda **kw: make_response(content="x"), supports_tools=False)

    with (
        mock.patch.object(llm, "_get_litellm", return_value=fake),
        pytest.raises(llm.LLMToolsUnsupported, match="does not support tool calling"),
    ):
        llm.complete(model="anthropic/claude-opus-4-8", prompt="hi", tools=[WEATHER_TOOL])


@pytest.mark.django_db
def test_an_unknown_model_is_given_the_benefit_of_the_doubt(llm_settings, api_key):
    """litellm not recognising a model is not a reason to refuse to run."""

    def raises(model):
        raise KeyError(model)

    fake = make_fake_litellm(lambda **kw: make_response(content="ok"))
    fake.supports_function_calling = raises

    with mock.patch.object(llm, "_get_litellm", return_value=fake):
        result = llm.complete(model="anthropic/claude-opus-4-8", prompt="hi", tools=[WEATHER_TOOL])

    assert result.text == "ok"


# --------------------------------------------------------------------------
# Malformed replies
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_malformed_tool_arguments_become_an_empty_set(llm_settings, api_key):
    """A model emitting broken JSON is routine, not exceptional.

    Empty arguments fail the tool's own schema check, which is reported back to
    the model as something it can correct — better than failing the run.
    """
    response = make_response(
        tool_calls=[make_tool_call("get_weather", "{not json")],
        finish_reason="tool_calls",
    )

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(lambda **kw: response)):
        result = llm.complete(model="anthropic/claude-opus-4-8", prompt="weather?", tools=[WEATHER_TOOL])

    assert result.tool_calls[0].arguments == {}


@pytest.mark.django_db
def test_a_tool_request_under_a_schema_is_not_parsed_as_the_answer(llm_settings, api_key):
    """A reply asking for tools carries no answer, schema or not."""
    response = make_response(
        tool_calls=[make_tool_call("get_weather", '{"city": "Berlin"}')],
        finish_reason="tool_calls",
    )
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False}

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(lambda **kw: response)):
        result = llm.complete(
            model="anthropic/claude-opus-4-8", prompt="weather?", schema=schema, tools=[WEATHER_TOOL]
        )

    assert result.json is None
    assert result.wants_tools


# --------------------------------------------------------------------------
# The multi-turn form
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_messages_are_passed_through_untouched(llm_settings, api_key):
    """An agent's conversation includes assistant turns and tool results."""
    conversation = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "get_weather"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]
    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        return make_response(content="Sunny in Berlin.")

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        result = llm.complete(model="anthropic/claude-opus-4-8", messages=conversation)

    assert seen["messages"] == conversation
    assert result.text == "Sunny in Berlin."


@pytest.mark.django_db
def test_prompt_and_messages_together_are_refused(llm_settings, api_key):
    with pytest.raises(llm.LLMError, match="not both and not neither"):
        llm.complete(model="anthropic/claude-opus-4-8", prompt="hi", messages=[{"role": "user", "content": "hi"}])


@pytest.mark.django_db
def test_neither_prompt_nor_messages_is_refused(llm_settings, api_key):
    with pytest.raises(llm.LLMError, match="not both and not neither"):
        llm.complete(model="anthropic/claude-opus-4-8")


@pytest.mark.django_db
def test_the_single_prompt_form_still_builds_its_own_messages(llm_settings, api_key):
    """The LLM Prompt action's signature must keep working unchanged."""
    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        return make_response(content="ok")

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        llm.complete(model="anthropic/claude-opus-4-8", prompt="hi", system="be brief")

    assert seen["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]

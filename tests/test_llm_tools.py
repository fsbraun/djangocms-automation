"""Tool calling in the provider-independent LLM wrapper (phase 1.1).

Everything here runs against a stubbed provider. No test may reach a network or
need credentials — an agent's failure paths are mostly things a live provider
will not do on demand, so they have to be scripted.
"""

import json
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


def _allow_deepseek(settings):
    """A provider that really does refuse an output shape, allowed and keyed."""
    settings.AUTOMATION_LLM_MODELS = [("deepseek/deepseek-chat", "DeepSeek Chat")]
    APIKey.objects.create(name="DeepSeek", service="deepseek", api_key="sk-test", is_active=True)


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


def test_an_unrecognised_finish_reason_counts_as_incomplete():
    """Read as an allow-list, so a new way of stopping early fails loudly.

    ``model_context_window_exceeded`` is a real one. A deny-list of the reasons
    known to be bad would have let it through as a finished answer.
    """
    from djangocms_automation.ai.llm import LLMResult

    def reply(reason):
        return LLMResult(text="half an answer", json=None, model="m", finish_reason=reason)

    assert reply("stop").incomplete is None
    assert reply("tool_calls").incomplete is None, "asking for a tool is a complete turn"
    assert reply("").incomplete is None, "not every provider sends one"
    assert "token limit" in reply("length").incomplete
    assert reply("model_context_window_exceeded").incomplete is not None


def test_unparseable_tool_arguments_stay_distinguishable_from_none():
    """``{}`` is a legitimate call for a tool whose fields are all optional, so
    a parse failure must not be spelled the same way."""
    from djangocms_automation.ai.llm import _tool_calls_from

    message = types.SimpleNamespace(
        tool_calls=[
            types.SimpleNamespace(id="c1", function=types.SimpleNamespace(name="t", arguments='{"a": ')),
            types.SimpleNamespace(id="c2", function=types.SimpleNamespace(name="t", arguments="{}")),
        ]
    )
    garbled, empty = _tool_calls_from(message)

    assert garbled.malformed is True
    assert garbled.arguments == {}
    assert empty.malformed is False, "an empty object is a real, valid call"


@pytest.mark.django_db
def test_a_provider_refusing_an_output_shape_says_what_to_change(llm_settings, api_key):
    """The real message from a real run, against a real provider.

    DeepSeek refuses ``response_format`` with a schema, and litellm's own
    capability data says it supports one — so this is recognised from the
    refusal rather than predicted. What reaches the editor should name the
    setting to change, not quote a provider's JSON at them.
    """

    def refuse(**kwargs):
        raise ValueError(
            'DeepseekException - {"error":{"message":"This response_format type is unavailable now",'
            '"type":"invalid_request_error"}}'
        )

    _allow_deepseek(llm_settings)
    fake = make_fake_litellm(refuse)

    with (
        mock.patch.object(llm, "_get_litellm", return_value=fake),
        pytest.raises(llm.LLMOutputShapeUnsupported, match="Remove the step's Output shape"),
    ):
        llm.complete(model="deepseek/deepseek-chat", prompt="hi", schema={"type": "object"})


@pytest.mark.django_db
def test_an_unrelated_failure_is_not_blamed_on_the_output_shape(llm_settings, api_key):
    """Sending someone to change the wrong setting is worse than saying little."""

    def fail(**kwargs):
        raise ValueError("insufficient balance")

    _allow_deepseek(llm_settings)
    fake = make_fake_litellm(fail)

    with mock.patch.object(llm, "_get_litellm", return_value=fake), pytest.raises(llm.LLMError) as raised:
        llm.complete(model="deepseek/deepseek-chat", prompt="hi", schema={"type": "object"})

    assert not isinstance(raised.value, llm.LLMOutputShapeUnsupported)


@pytest.mark.django_db
def test_a_refusal_without_an_output_shape_is_reported_as_itself(llm_settings, api_key):
    """No schema was sent, so no schema is the problem."""

    def refuse(**kwargs):
        raise ValueError("response_format is unavailable")

    _allow_deepseek(llm_settings)
    fake = make_fake_litellm(refuse)

    with mock.patch.object(llm, "_get_litellm", return_value=fake), pytest.raises(llm.LLMError) as raised:
        llm.complete(model="deepseek/deepseek-chat", prompt="hi")

    assert not isinstance(raised.value, llm.LLMOutputShapeUnsupported)


@pytest.mark.django_db
def test_a_refused_output_shape_is_asked_for_as_a_tool_instead(llm_settings, api_key):
    """DeepSeek will not take a schema as a response format. It will call a tool.

    So the schema goes as a function the provider is obliged to call, and the
    arguments it sends back are the answer — still checked by the provider,
    which is what the rows downstream are read on.
    """
    _allow_deepseek(llm_settings)
    seen = []

    def provider(**kwargs):
        seen.append(kwargs)
        if "response_format" in kwargs:
            raise ValueError(
                'DeepseekException - {"error":{"message":"This response_format type is unavailable now"}}'
            )
        call = make_tool_call(llm.SHAPE_TOOL_NAME, '{"topic": "billing"}')
        return make_response(tool_calls=[call], finish_reason="tool_calls")

    schema = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(provider)):
        result = llm.complete(model="deepseek/deepseek-chat", prompt="what is this about", schema=schema)

    assert result.json == {"topic": "billing"}, "the arguments are the answer"
    assert result.tool_calls == [], "nobody asked for a tool, so none is handed back"
    # The same reply a provider with native support would have sent, so the
    # transcript reads the same either way rather than looking like silence.
    assert json.loads(result.text) == {"topic": "billing"}
    assert result.finish_reason == "stop", "it answered; a cut-short reply is a different thing"

    asked = seen[1]
    assert "response_format" not in asked
    assert asked["tools"][0]["function"]["parameters"] == schema, "the same schema, carried differently"
    assert asked["tool_choice"]["function"]["name"] == llm.SHAPE_TOOL_NAME, "obliged, not invited"


@pytest.mark.django_db
def test_the_second_way_is_tried_first_next_time(llm_settings, api_key):
    """A refusal is a fact about the provider, so it is remembered.

    Paying for the same rejected call on every run would be a slow way of
    learning something already known.
    """
    _allow_deepseek(llm_settings)
    attempts = []

    def provider(**kwargs):
        attempts.append("native" if "response_format" in kwargs else "tool")
        if "response_format" in kwargs:
            raise ValueError("response_format type is unavailable now")
        return make_response(tool_calls=[make_tool_call(llm.SHAPE_TOOL_NAME, "{}")], finish_reason="tool_calls")

    schema = {"type": "object", "properties": {}, "required": []}
    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(provider)):
        llm.complete(model="deepseek/deepseek-chat", prompt="hi", schema=schema)
        llm.complete(model="deepseek/deepseek-chat", prompt="hi", schema=schema)

    assert attempts == ["native", "tool", "tool"], "refused once, then asked the right way"


@pytest.mark.django_db
def test_a_provider_that_can_do_neither_says_so_once(llm_settings, api_key):
    """No response format and no tools leaves nothing to try."""
    _allow_deepseek(llm_settings)

    def provider(**kwargs):
        if "response_format" in kwargs:
            raise ValueError("response_format type is unavailable now")
        raise ValueError("this model does not support tools")

    with (
        mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(provider)),
        pytest.raises(llm.LLMOutputShapeUnsupported, match="as a response format or as a tool"),
    ):
        llm.complete(model="deepseek/deepseek-chat", prompt="hi", schema={"type": "object"})


@pytest.mark.django_db
def test_a_step_with_real_tools_is_left_alone(llm_settings, api_key):
    """A step offering tools ignores its output shape by design.

    Retrying its refusal as a forced tool call would replace the tools the
    editor chose with one of our own.
    """
    _allow_deepseek(llm_settings)

    def provider(**kwargs):
        raise ValueError("response_format type is unavailable now")

    with (
        mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(provider)),
        pytest.raises(llm.LLMError) as raised,
    ):
        llm.complete(model="deepseek/deepseek-chat", prompt="hi", schema={"type": "object"}, tools=[WEATHER_TOOL])

    assert not isinstance(raised.value, llm.LLMOutputShapeUnsupported)
    assert "deepseek/deepseek-chat" not in llm._SHAPE_NEEDS_A_TOOL

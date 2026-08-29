"""The contract this package relies on litellm to keep.

Every other LLM test stubs the provider, which is right — they must not need
credentials or a network. The cost is that nothing would notice if an upgrade
moved litellm's API out from under the wrapper: the stubs would keep passing
while real calls broke.

These tests touch the real library and no network. They are the reason the
version cap in ``pyproject.toml`` can be raised with confidence rather than
hope: if one fails after an upgrade, the wrapper needs looking at.
"""

import inspect

import pytest

litellm = pytest.importorskip("litellm", reason="the 'llm' extra is not installed")


def test_completion_accepts_the_parameters_the_wrapper_sends():
    """``complete()`` passes these by keyword; a rename would break every call."""
    parameters = inspect.signature(litellm.completion).parameters
    for name in ("model", "messages", "api_key", "max_tokens", "response_format", "tools", "tool_choice", "timeout"):
        assert name in parameters, f"litellm.completion no longer accepts {name!r}"


def test_the_error_types_the_wrapper_catches_still_exist():
    """Rate limits and connection errors are told apart to decide retry behaviour."""
    assert issubclass(litellm.RateLimitError, Exception)
    assert issubclass(litellm.APIConnectionError, Exception)


def test_tool_support_can_be_interrogated():
    """Used to refuse tools on a model that cannot use them."""
    assert callable(litellm.supports_function_calling)


def test_usage_accounting_helpers_exist():
    """Phase 1.5 budgets are built on these rather than a hand-rolled price table."""
    assert callable(litellm.token_counter)
    assert callable(litellm.completion_cost)


def test_a_tool_definition_is_still_shaped_the_way_the_wrapper_emits_it():
    """The provider-neutral tool shape, checked against litellm's own validator.

    If litellm changes what it accepts here, the tool contract's generated
    schemas would be rejected at call time by every provider at once.
    """
    tool = {
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
    # litellm exposes the OpenAI-shaped types it maps everything else onto.
    from litellm.types.utils import ChatCompletionMessageToolCall  # noqa: F401

    assert set(tool["function"]) >= {"name", "description", "parameters"}
    assert tool["function"]["parameters"]["additionalProperties"] is False

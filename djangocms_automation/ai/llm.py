"""Provider-independent LLM access for automation actions.

Built on `LiteLLM <https://docs.litellm.ai/>`_ — one ``completion()`` API
across Anthropic, OpenAI, Google, Mistral, Azure, Bedrock, Ollama and many
more providers. Model strings use LiteLLM's ``"<provider>/<model>"``
convention (e.g. ``anthropic/claude-opus-4-8``, ``openai/gpt-4.1``).

This module wraps LiteLLM behind a small internal contract
(:func:`complete` / :class:`LLMResult`) so action code — and any future
backend swap — depends only on this package's own API. API keys come from
the :class:`~djangocms_automation.models.APIKey` secrets store; the key is
looked up by the provider prefix of the model string, which aligns with the
``service_registry`` ids (``anthropic``, ``openai``, ``google``, ...).

Settings:

* ``AUTOMATION_LLM_MODELS`` — list of model strings offered in the LLM
  action's model choice field (default ``[]`` — deny-all, mirroring
  ``AUTOMATION_ALLOWED_MODELS``).
* ``AUTOMATION_LLM_DEFAULT`` — optional preselected model string.

Install the optional dependency with ``pip install djangocms-automation[llm]``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

__all__ = [
    "COMPLETE_FINISH_REASONS",
    "LLMError",
    "LLMRateLimited",
    "LLMResult",
    "LLMToolsUnsupported",
    "ToolCall",
    "complete",
    "get_allowed_llm_models",
    "get_api_key",
]


class LLMError(Exception):
    """A non-retryable LLM failure (configuration, API, or network error)."""


class LLMToolsUnsupported(LLMError):
    """The configured model cannot be given tools.

    Raised rather than dropping the tools and completing anyway: an agent whose
    tools were silently ignored produces confident prose instead of doing the
    work, which is far harder to diagnose than a configuration error.
    """


class LLMRateLimited(LLMError):
    """The provider rate-limited the request; retry after ``retry_after`` seconds."""

    def __init__(self, retry_after: int = 60, message: str = "Rate limited"):
        self.retry_after = max(int(retry_after), 1)
        super().__init__(message)


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation the model asked for.

    Lives here rather than with the tool contract because it is a property of
    the model's reply: the wrapper's job is to turn each provider's shape into
    this one. ``arguments`` is whatever the model produced and is **untrusted**
    — the tool contract validates it before anything runs.
    """

    id: str
    name: str
    arguments: dict[str, Any]
    #: The model sent arguments that were not valid JSON. ``arguments`` is then
    #: empty, which is *not* the same as the model having sent none: for a tool
    #: whose inputs are all optional, an empty object is a valid call meaning
    #: "use the defaults". A garbled message must not be able to mean that.
    malformed: bool = False


#: Reasons that mean the model stopped because it had finished. ``tool_calls``
#: is here because asking for a tool *is* a complete turn; the empty string is
#: an absent reason, which not every provider sends.
COMPLETE_FINISH_REASONS = frozenset({"", "stop", "stop_sequence", "end_turn", "tool_calls", "function_call"})

#: What to tell someone about the reasons that are not.
_INCOMPLETE_REASONS = {
    "length": "the reply was cut off at the model's token limit",
    "max_tokens": "the reply was cut off at the model's token limit",
    "content_filter": "the provider filtered the reply",
}


def _incomplete_because(reason: str) -> str:
    return _INCOMPLETE_REASONS.get(reason, f"the model stopped for an unrecognised reason ({reason!r})")


@dataclass
class LLMResult:
    """Normalized result of an LLM completion."""

    text: str
    json: Any | None
    model: str
    usage: dict = field(default_factory=dict)
    #: Tool invocations the model requested, in the order it asked for them.
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: Why the model stopped - ``"tool_calls"``, ``"stop"``, ``"length"``, ...
    finish_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        """Whether the model is asking to run tools rather than answering."""
        return bool(self.tool_calls)

    @property
    def incomplete(self) -> str | None:
        """Why this reply is not a whole answer, or ``None`` if it is one.

        A truncated reply is the failure mode that does not look like one. It is
        fluent to its last word and simply stops, so nothing reading it
        afterwards can tell it apart from an answer — not a schema, not a
        downstream action, not a person skimming the output. The provider says
        so in one field, and that field is the only chance anyone gets.

        Read as an allow-list. Anything not known to mean "the model said what
        it meant to say" is treated as incomplete, so a provider inventing a new
        way to stop early fails loudly rather than passing silently. An absent
        reason stays acceptable: not every provider sends one, and inventing a
        failure from missing information would break working setups.
        """
        return None if self.finish_reason in COMPLETE_FINISH_REASONS else _incomplete_because(self.finish_reason)


def get_allowed_llm_models() -> list[str]:
    """Get the model strings automations may use."""
    return list(getattr(settings, "AUTOMATION_LLM_MODELS", []))


def get_api_key(service: str) -> str:
    """Look up the active API key for a provider from the secrets store.

    :raises LLMError: If no active key is stored for the service.
    """
    from ..models import APIKey

    api_key = APIKey.objects.filter(service=service, is_active=True).order_by("-updated").first()
    if api_key is None:
        raise LLMError(
            f"No active API key stored for service '{service}'. Add one under Automations → Secrets in the admin."
        )
    return api_key.api_key


def _get_litellm():
    try:
        import litellm
    except ImportError as exc:  # pragma: no cover - exercised via unit test mock
        raise LLMError(
            "The 'litellm' package is required for LLM actions. Install it with: pip install djangocms-automation[llm]"
        ) from exc
    return litellm


def _tool_calls_from(message) -> list[ToolCall]:
    """Normalize a provider's tool-call shape into :class:`ToolCall` objects.

    Arguments arrive as a JSON string. A model that emits malformed JSON is a
    normal occurrence, not an exception. The call is kept and marked
    ``malformed``, so the tool contract can report a correctable error to the
    model rather than failing the run — and so that it stays distinguishable
    from a call that legitimately carried no arguments.
    """
    calls = []
    for raw in getattr(message, "tool_calls", None) or []:
        function = getattr(raw, "function", None)
        if function is None:
            continue
        raw_arguments = getattr(function, "arguments", None) or "{}"
        malformed = False
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
        except (json.JSONDecodeError, TypeError, ValueError):
            arguments, malformed = {}, True
        if not isinstance(arguments, dict):
            arguments, malformed = {}, True
        calls.append(
            ToolCall(
                id=str(getattr(raw, "id", "") or ""),
                name=str(getattr(function, "name", "") or ""),
                arguments=arguments,
                malformed=malformed,
            )
        )
    return calls


def _supports_tools(litellm, model: str) -> bool:
    """Check whether a model can be given tools, tolerating an unknown model."""
    try:
        return bool(litellm.supports_function_calling(model=model))
    except Exception:  # noqa: BLE001 — an unrecognised model is not a reason to refuse
        return True


def complete(
    *,
    model: str,
    prompt: str | None = None,
    system: str | None = None,
    messages: list[dict] | None = None,
    schema: dict | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    max_tokens: int = 16000,
    timeout: float | None = None,
) -> LLMResult:
    """Run one LLM completion, optionally offering it tools.

    Two ways to call it. ``prompt`` (with an optional ``system``) is the
    single-turn form the LLM Prompt action uses. ``messages`` is the multi-turn
    form an agent needs, carrying the conversation so far — including the
    assistant's previous tool requests and the results that came back.

    :param model: LiteLLM model string, ``"<provider>/<model>"``. Must be in
        ``AUTOMATION_LLM_MODELS``.
    :param prompt: The user prompt, for the single-turn form.
    :param system: Optional system prompt, for the single-turn form.
    :param messages: The conversation so far, for the multi-turn form. Mutually
        exclusive with ``prompt``.
    :param schema: Optional JSON schema; when given, the response is
        constrained to valid JSON matching it and parsed into ``result.json``.
    :param tools: Tool definitions the model may call, in the provider-neutral
        ``{"type": "function", "function": {...}}`` shape.
    :param tool_choice: ``"auto"``, ``"none"``, ``"required"``, or a specific
        tool. Left to the provider's default when omitted.
    :param max_tokens: Response token cap.
    :param timeout: Seconds to wait for the provider. Without one a hung call
        holds its worker — and its action's lease — until something else gives
        up, so an agent should always set it.
    :raises LLMToolsUnsupported: If tools are given to a model that cannot use them.
    :raises LLMRateLimited: On provider rate limits (retry later).
    :raises LLMError: On any other provider/configuration error.
    """
    if (prompt is None) == (messages is None):
        raise LLMError("Pass either 'prompt' or 'messages' to complete(), not both and not neither.")

    allowed = get_allowed_llm_models()
    if model not in allowed:
        raise LLMError(f"Model '{model}' is not allowed for automations. Add it to the AUTOMATION_LLM_MODELS setting.")
    service = model.split("/", 1)[0]
    api_key = get_api_key(service)
    litellm = _get_litellm()

    if tools and not _supports_tools(litellm, model):
        raise LLMToolsUnsupported(
            f"Model '{model}' does not support tool calling. Choose a model that does, or remove the tools."
        )

    if messages is None:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if schema:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": schema, "strict": True},
        }
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    if timeout is not None:
        kwargs["timeout"] = timeout

    try:
        response = litellm.completion(**kwargs)
    except litellm.RateLimitError as exc:
        retry_after = 60
        headers = getattr(exc, "response", None)
        if headers is not None:
            try:
                retry_after = int(exc.response.headers.get("retry-after", "60"))
            except (TypeError, ValueError, AttributeError):
                retry_after = 60
        raise LLMRateLimited(retry_after, str(exc)) from exc
    except litellm.APIConnectionError as exc:
        raise LLMError(f"Network error calling '{service}': {exc}") from exc
    except Exception as exc:  # litellm maps provider errors to OpenAI-style exceptions
        raise LLMError(f"LLM error from '{service}': {exc}") from exc

    choice = response.choices[0]
    message = choice.message
    text = getattr(message, "content", None) or ""
    tool_calls = _tool_calls_from(message)

    parsed = None
    # A reply that asks for tools carries no answer to parse, even under a schema.
    if schema and not tool_calls:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model returned invalid JSON for the requested schema: {exc}") from exc

    usage = {}
    raw_usage = getattr(response, "usage", None)
    if raw_usage is not None:
        usage = {
            "input_tokens": getattr(raw_usage, "prompt_tokens", None),
            "output_tokens": getattr(raw_usage, "completion_tokens", None),
        }
    return LLMResult(
        text=text,
        json=parsed,
        model=getattr(response, "model", model),
        usage=usage,
        tool_calls=tool_calls,
        finish_reason=str(getattr(choice, "finish_reason", "") or ""),
    )

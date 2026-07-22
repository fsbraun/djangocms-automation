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
    "LLMError",
    "LLMRateLimited",
    "LLMResult",
    "complete",
    "get_api_key",
    "get_allowed_llm_models",
]


class LLMError(Exception):
    """A non-retryable LLM failure (configuration, API, or network error)."""


class LLMRateLimited(LLMError):
    """The provider rate-limited the request; retry after ``retry_after`` seconds."""

    def __init__(self, retry_after: int = 60, message: str = "Rate limited"):
        self.retry_after = max(int(retry_after), 1)
        super().__init__(message)


@dataclass
class LLMResult:
    """Normalized result of an LLM completion."""

    text: str
    json: Any | None
    model: str
    usage: dict = field(default_factory=dict)


def get_allowed_llm_models() -> list[str]:
    """Get the model strings automations may use."""
    return list(getattr(settings, "AUTOMATION_LLM_MODELS", []))


def get_api_key(service: str) -> str:
    """Look up the active API key for a provider from the secrets store.

    :raises LLMError: If no active key is stored for the service.
    """
    from .models import APIKey

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


def complete(
    *,
    model: str,
    prompt: str,
    system: str | None = None,
    schema: dict | None = None,
    max_tokens: int = 16000,
) -> LLMResult:
    """Run a single LLM completion.

    :param model: LiteLLM model string, ``"<provider>/<model>"``. Must be in
        ``AUTOMATION_LLM_MODELS``.
    :param prompt: The user prompt.
    :param system: Optional system prompt.
    :param schema: Optional JSON schema; when given, the response is
        constrained to valid JSON matching it and parsed into ``result.json``.
    :param max_tokens: Response token cap.
    :raises LLMRateLimited: On provider rate limits (retry later).
    :raises LLMError: On any other provider/configuration error.
    """
    allowed = get_allowed_llm_models()
    if model not in allowed:
        raise LLMError(f"Model '{model}' is not allowed for automations. Add it to the AUTOMATION_LLM_MODELS setting.")
    service = model.split("/", 1)[0]
    api_key = get_api_key(service)
    litellm = _get_litellm()

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

    text = response.choices[0].message.content or ""
    parsed = None
    if schema:
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
    return LLMResult(text=text, json=parsed, model=getattr(response, "model", model), usage=usage)

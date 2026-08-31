"""A model that is not a model, for trying an AI step without a provider.

Building an agent means getting several things right at once — what the tools
are, which of their inputs the model may fill, what needs approving, what the
downstream steps do with the answer — and none of it is easy to see until a run
happens. Waiting on an API key, a bill and a nondeterministic model to find out
that a filter was bound the wrong way round is a poor way to spend an afternoon.

So: a model string beginning ``dummy/`` is answered here instead. No network, no
key, no ``litellm``, and the same answer every time.

Enabling it is the ordinary opt-in — put it in the allowlist:

.. code-block:: python

    AUTOMATION_LLM_MODELS = [("dummy/echo", "Echo (no provider)")]

It does nothing unless a step is configured to use it, and it cannot be reached
by a step that names a real model.

Directing it
------------

By default it replies with the task it was given, which is enough to check that
prompts render, data flows on, and the step completes. Put a directive on its
own line in the **Task** to make it do something:

.. code-block:: text

    Look this customer up and reply to them.

    !call find_customer {"filters": {"email": "ann@example.com"}}
    !call reply_to_customer {"subject": "Hello", "body": "Thanks for writing."}

Each ``!call`` is made in turn, one per turn, with the observation fed back
exactly as a real model would see it — so approval gates pause, failures come
back as errors, and the transcript reads the way a real one does. When the calls
are done it answers with everything it saw.

``!json {...}`` makes it answer with that object instead of text, for trying an
**Output shape** without a provider that honours one. ``!fail some message``
makes the provider call fail, for trying what a step does when it cannot answer.
Arguments that are not valid JSON — ``!call find_users {oops}`` — come through
marked as such, so the refusal that a real garbled reply gets can be tried too.
"""

from __future__ import annotations

import json
import re

from ..tools import ToolCall

__all__ = ["DUMMY_PREFIX", "answer", "is_dummy"]

#: Model strings handled here rather than sent anywhere.
DUMMY_PREFIX = "dummy/"

_CALL = re.compile(r"^\s*!call\s+([A-Za-z0-9_-]+)\s*(\{.*\})?\s*$", re.MULTILINE)
_JSON = re.compile(r"^\s*!json\s+(\{.*\})\s*$", re.MULTILINE)
_FAIL = re.compile(r"^\s*!fail\s*(.*)$", re.MULTILINE)


def is_dummy(model: str) -> bool:
    """Whether this model string is answered locally."""
    return str(model or "").startswith(DUMMY_PREFIX)


def answer(model: str, messages: list[dict], tools: list | None):
    """Reply as a model would, from directives in the conversation.

    :returns: An ``LLMResult``, built here rather than imported at module level
        because the wrapper imports this module.
    :raises LLMError: When the task asked for a failure with ``!fail``.
    """
    from .llm import LLMError, LLMResult

    task = _first_user_message(messages)

    failure = _FAIL.search(task)
    if failure:
        raise LLMError(failure.group(1).strip() or "The dummy model was asked to fail.")

    wanted = _CALL.findall(task)
    answered = {message.get("tool_call_id") for message in messages if message.get("role") == "tool"}
    offered = {tool["function"]["name"] for tool in tools or []}

    # One call per turn, in the order they were written, skipping any already
    # answered — which is what makes the transcript look like a real one.
    for index, (name, raw) in enumerate(wanted):
        call_id = f"dummy-{index + 1}"
        if call_id in answered:
            continue
        if name not in offered:
            # Left to the step to report, exactly as a real model's mistake
            # would be: it is told what it actually has and asked again.
            pass
        malformed = False
        try:
            arguments = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            # Marked, not silently emptied. A real provider's garbled arguments
            # are refused before the tool runs, and a stand-in that quietly
            # turned them into "no arguments" would let a tool whose inputs are
            # all optional run — which is the very thing that refusal prevents.
            arguments, malformed = {}, True
        return LLMResult(
            text="",
            json=None,
            model=model,
            usage={"input_tokens": len(task) // 4, "output_tokens": 8},
            tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments, malformed=malformed)],
            finish_reason="tool_calls",
        )

    shape = _JSON.search(task)
    if shape:
        try:
            parsed = json.loads(shape.group(1))
        except json.JSONDecodeError as exc:
            raise LLMError(f"The !json directive is not valid JSON: {exc}") from exc
        return LLMResult(
            text="", json=parsed, model=model, usage={"input_tokens": 0, "output_tokens": 0}, finish_reason="stop"
        )

    return LLMResult(
        text=_summary(task, messages),
        json=None,
        model=model,
        usage={"input_tokens": len(task) // 4, "output_tokens": 16},
        finish_reason="stop",
    )


def _first_user_message(messages: list[dict]) -> str:
    for message in messages or []:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _summary(task: str, messages: list[dict]) -> str:
    """What it says when it has nothing left to do.

    The task with its directives stripped, plus what the tools returned — so an
    editor can see at a glance that the prompt rendered and the calls landed.
    """
    said = _FAIL.sub("", _JSON.sub("", _CALL.sub("", task))).strip()
    observations = [str(message.get("content") or "") for message in messages or [] if message.get("role") == "tool"]
    if not observations:
        return said or "The dummy model has nothing to say."
    return f"{said}\n\nTools returned: " + " | ".join(observations)

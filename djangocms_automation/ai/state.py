"""An agent's working state, kept across the engine's re-entries.

An agent is a re-entrant node: it runs one turn, suspends while a tool call
executes as its own action, and is woken to run the next turn. Between those
turns it holds a conversation, a count of what it has spent, and a note of which
tool calls it has already dispatched. None of that can live in memory, because
the process that resumes the agent may not be the process that suspended it —
and may be on another machine.

It lives in ``AutomationAction.scratch``, a field belonging to the node alone.
The obvious alternative, ``result``, is not private: failure propagation
overwrites it with ``{"failed_action_id": ...}`` when something below fails. Two
nodes already work around that by deriving their state from the action tree —
the loop counts its iterations, the conditional recovers its branch from the
action it spawned. An agent cannot: a conversation is not derivable from
anything. So it gets a field.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .llm import ToolCall

__all__ = ["AgentState"]


@dataclass
class AgentState:
    """What an agent needs to remember between turns.

    Serialized to and from ``AutomationAction.scratch``. Everything here must
    be JSON, because it crosses a process boundary on every turn.
    """

    #: The conversation so far, in the provider-neutral message shape.
    messages: list[dict] = field(default_factory=list)
    #: Turns taken. One turn is one model call plus the tool calls it asked for.
    turn: int = 0
    #: Tool calls completed, across all turns.
    tool_calls: int = 0
    #: Accumulated token usage, summed over every turn.
    usage: dict[str, int] = field(default_factory=dict)
    #: When the run began, ISO-8601, for the wall-clock budget.
    started_at: str = ""
    #: Calls the model has asked for that have not been dispatched yet.
    pending: list[dict] = field(default_factory=list)
    #: Calls already turned into actions. A woken agent must not spawn a tool
    #: call twice because it could not remember doing it the first time.
    dispatched: list[str] = field(default_factory=list)

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, action) -> AgentState:
        """Read the state from an action, tolerating a node that has none yet."""
        raw = action.scratch if isinstance(action.scratch, dict) else {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in raw.items() if key in known})

    def save(self, action) -> None:
        """Write the state back, without disturbing anything else on the row."""
        from djangocms_automation.instances import AutomationAction

        payload = asdict(self)
        AutomationAction.objects.filter(pk=action.pk).update(scratch=payload)
        action.scratch = payload

    # -- the conversation --------------------------------------------------

    def start(self, system: str | None, prompt: str) -> None:
        """Seed the conversation for the first turn."""
        self.messages = []
        if system:
            self.messages.append({"role": "system", "content": system})
        self.messages.append({"role": "user", "content": prompt})

    def record_reply(self, result) -> None:
        """Append the model's turn, and count what it cost.

        A reply asking for tools has to be kept verbatim: providers require the
        assistant's tool request to be present in the conversation before the
        results that answer it, or the next call is rejected as inconsistent.
        """
        message: dict[str, Any] = {"role": "assistant", "content": result.text or ""}
        if result.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": _dump(call.arguments)},
                }
                for call in result.tool_calls
            ]
        self.messages.append(message)
        self.turn += 1
        for key, value in (result.usage or {}).items():
            if isinstance(value, int):
                self.usage[key] = self.usage.get(key, 0) + value

    def record_observation(self, call_id: str, content: str, is_error: bool = False) -> None:
        """Append what a tool returned, as the answer to one request."""
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": content if not is_error else f"Error: {content}",
            }
        )
        self.tool_calls += 1

    # -- dispatch bookkeeping ---------------------------------------------

    def queue(self, calls: list[ToolCall]) -> None:
        """Record the calls a turn asked for, ready to be dispatched."""
        self.pending = [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in calls]

    def undispatched(self) -> list[ToolCall]:
        """The queued calls not yet turned into actions.

        Consulted on every re-entry, which is what stops a woken agent
        dispatching a tool call it already dispatched — the difference between
        a resumed agent and a duplicated side effect.
        """
        return [
            ToolCall(id=entry["id"], name=entry["name"], arguments=entry.get("arguments") or {})
            for entry in self.pending
            if entry["id"] not in self.dispatched
        ]

    def mark_dispatched(self, calls: list[ToolCall]) -> None:
        for call in calls:
            if call.id not in self.dispatched:
                self.dispatched.append(call.id)

    @property
    def total_tokens(self) -> int:
        """Every token spent so far, for the budget to measure itself against."""
        return sum(value for value in self.usage.values() if isinstance(value, int))


def _dump(arguments) -> str:
    """Render tool arguments the way providers expect them in a transcript."""
    import json

    try:
        return json.dumps(arguments)
    except (TypeError, ValueError):
        return "{}"

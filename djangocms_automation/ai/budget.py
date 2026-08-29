"""Limits on what one agent run may spend.

An agent decides its own next step, so nothing about its cost is knowable in
advance: a loop bound counts iterations a workflow author wrote down, but an
agent's turn count is chosen by a model. Every limit here is therefore enforced
rather than advised, and exhausting one is a terminal, inspectable failure — not
a quiet stop. An agent that silently gave up half way would return a confident
partial answer, which is worse than an error because nothing marks it as wrong.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.utils.timezone import now

__all__ = ["AgentBudget", "BudgetExceeded"]


class BudgetExceeded(Exception):
    """An agent reached one of its limits and must stop."""


@dataclass(frozen=True)
class AgentBudget:
    """The four ways an agent run is bounded, plus the size of one observation.

    They are separate because they fail differently. Turns catch a model that
    will not conclude; tool calls catch one that thrashes between them; tokens
    catch a conversation growing faster than it progresses; the deadline catches
    everything else, including a provider that has become very slow.
    """

    max_turns: int = 10
    max_tool_calls: int = 25
    max_tokens: int = 200_000
    deadline_seconds: int = 900
    #: Per-observation cap. One tool returning a thousand rows would otherwise
    #: spend the whole context window on a single answer.
    max_observation_chars: int = 8_000

    def check(self, state) -> None:
        """Raise if this run has spent anything it is not allowed to.

        Checked before each turn, so an agent stops at its limit rather than
        one step past it.
        """
        if self.max_turns and state.turn >= self.max_turns:
            raise BudgetExceeded(
                f"Agent stopped after {state.turn} turns without reaching an answer. "
                f"Raise the turn limit, or narrow what it is being asked to do."
            )
        if self.max_tool_calls and state.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded(
                f"Agent stopped after {state.tool_calls} tool calls. "
                f"Its tools may not be giving it what it needs to finish."
            )
        if self.max_tokens and state.total_tokens >= self.max_tokens:
            raise BudgetExceeded(
                f"Agent stopped after {state.total_tokens} tokens. "
                f"Long conversations grow the cost of every following turn."
            )
        if self.deadline_seconds and state.started_at:
            elapsed = (now() - _parse(state.started_at)).total_seconds()
            if elapsed > self.deadline_seconds:
                raise BudgetExceeded(
                    f"Agent stopped after {int(elapsed)} seconds, over its {self.deadline_seconds} second limit."
                )

    def allow(self, state, calls: list) -> list:
        """Trim a turn's tool calls to what this run may still spend.

        The tool-call limit bounds *side effects*, so it has to be applied
        before the calls are dispatched. Checking it at the start of the next
        turn is checking it after they have already run — which is the thing the
        limit exists to prevent. A model that asks for three calls with one left
        gets one, and is told the other two did not run.

        :raises BudgetExceeded: If none of them may run.
        """
        if not self.max_tool_calls:
            return list(calls)
        remaining = max(self.max_tool_calls - state.tool_calls, 0)
        if remaining == 0 and calls:
            raise BudgetExceeded(
                f"Agent stopped after {state.tool_calls} tool calls. "
                f"Its tools may not be giving it what it needs to finish."
            )
        return list(calls)[:remaining]


def _parse(timestamp: str):
    """Read an ISO timestamp, falling back to now if it is unreadable."""
    try:
        parsed = datetime.datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed

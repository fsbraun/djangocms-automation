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
    will not conclude; tokens catch a conversation growing faster than it
    progresses; the deadline catches everything else, including a provider that
    has become very slow. Those three end the run.

    Tool calls are the exception and bound something else: side effects. A call
    over the limit is refused rather than fatal, because the model can still
    answer from what it has — see :meth:`allow`.
    """

    max_turns: int = 10
    max_tool_calls: int = 25
    max_tokens: int = 200_000
    deadline_seconds: int = 900
    #: Per-observation cap. One tool returning a thousand rows would otherwise
    #: spend the whole context window on a single answer.
    max_observation_chars: int = 8_000

    def remaining_seconds(self, state) -> float | None:
        """Wall clock left, or ``None`` when no deadline was set.

        A provider call must not be allowed to outlast the run it belongs to:
        a request given longer than the deadline is a request whose answer
        arrives after the run was supposed to have failed.
        """
        if not self.deadline_seconds or not state.started_at:
            return None
        return self.deadline_seconds - (now() - _parse(state.started_at)).total_seconds()

    def check_spend(self, state) -> None:
        """Raise on the limits a turn spends *while* taking it.

        Tokens and wall clock are only known after a call returns, so checking
        them beforehand leaves the last turn free to exceed either and finish
        anyway — a run that went over its limit and reported success.
        """
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
        self.check_spend(state)

    def allow(self, state, calls: list) -> list:
        """Trim a turn's tool calls to what this run may still spend.

        The tool-call limit bounds *side effects*, so it has to be applied
        before the calls are dispatched. Checking it at the start of the next
        turn is checking it after they have already run — which is the thing the
        limit exists to prevent. A model that asks for three calls with one left
        gets one, and is told the other two did not run.

        This is the *only* thing the tool-call limit does. It does not fail the
        run: refusing a call and saying so leaves the model a turn to answer
        with what it already has, where failing would throw away work that was
        within budget. What stops a model that will not conclude is the turn
        limit, which is the limit for that.
        """
        if not self.max_tool_calls:
            return list(calls)
        remaining = max(self.max_tool_calls - state.tool_calls, 0)
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

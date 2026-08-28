"""Signals emitted by the automation engine.

These are the supported extension points for metrics, tracing, and alerting.
Receivers must not raise: the engine logs and swallows receiver errors so that
an observability failure can never fail an execution.
"""

from __future__ import annotations

import django.dispatch

__all__ = [
    "action_transitioned",
    "action_dead_lettered",
    "instance_finished",
]

#: Sent after an action's state changes.
#:
#: :param sender: ``AutomationAction``
#: :param action: The action, already saved in its new state.
#: :param from_state: Previous state.
#: :param to_state: New state.
#: :param attempt: Attempt number after the transition.
#: :param duration: Seconds spent in ``RUNNING``, for terminal transitions.
action_transitioned = django.dispatch.Signal()

#: Sent when an action exhausts its attempts and is written to the dead letter.
action_dead_lettered = django.dispatch.Signal()

#: Sent when an instance reaches a terminal status.
instance_finished = django.dispatch.Signal()

"""Retry policy and failure classification for automation actions.

An action's failure is either *retryable* (a transient condition that a later
attempt may survive: a network blip, a locked row, a rate limit) or *permanent*
(a configuration error, a validation failure, a missing model field). Only
retryable failures consume an attempt and get rescheduled; permanent ones fail
fast, which is the engine's existing behavior.

Plugins declare a policy by setting ``retry_policy`` on the plugin model::

    class MyActionPluginModel(BaseActionPluginModel):
        retry_policy = RetryPolicy(max_attempts=5, backoff_seconds=10)

Classification is deliberately explicit. An unknown exception is *not* retried:
retrying an unknown failure risks repeating a side effect that already partially
happened. Actions opt in by raising :class:`RetryableError`, by listing
exception classes in ``retry_on``, or both.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_RETRY_POLICY",
    "PermanentError",
    "RetryPolicy",
    "RetryableError",
]


class RetryableError(Exception):
    """A transient failure. The engine reschedules the action if attempts remain.

    ``retry_after`` optionally overrides the policy's computed backoff, for
    providers that tell you exactly how long to wait.
    """

    def __init__(self, message: str = "", retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(message or "Retryable failure")


class PermanentError(Exception):
    """A failure that must never be retried, whatever the policy says.

    Takes precedence over ``retry_on``: raise this when a retry could only
    repeat a side effect or waste an attempt.
    """


@dataclass(frozen=True)
class RetryPolicy:
    """How an action retries.

    :param max_attempts: Total attempts including the first. ``1`` disables
        retrying, which is the default and matches historical behavior.
    :param backoff_seconds: Delay before the second attempt.
    :param backoff_multiplier: Growth factor applied per subsequent attempt.
    :param max_backoff_seconds: Ceiling for the computed delay.
    :param jitter: Fraction of the delay applied as random spread, so a fleet
        of workers recovering from the same outage does not retry in lockstep.
    :param retry_on: Additional exception classes treated as retryable.
    """

    max_attempts: int = 1
    backoff_seconds: float = 30.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 3600.0
    jitter: float = 0.25
    retry_on: tuple[type[BaseException], ...] = field(default_factory=tuple)

    def is_retryable(self, exc: BaseException) -> bool:
        """Check whether ``exc`` may be retried under this policy."""
        if isinstance(exc, PermanentError):
            return False
        if isinstance(exc, RetryableError):
            return True
        return bool(self.retry_on) and isinstance(exc, tuple(self.retry_on))

    def should_retry(self, exc: BaseException, attempt_count: int, max_attempts: int | None = None) -> bool:
        """Check whether an action on attempt ``attempt_count`` may run again.

        :param max_attempts: Overrides the policy's own limit, for the
            per-action override the engine resolves from the action row.
        """
        limit = self.max_attempts if max_attempts is None else max_attempts
        return attempt_count < limit and self.is_retryable(exc)

    def next_delay(self, attempt_count: int, exc: BaseException | None = None) -> float:
        """Compute the delay in seconds before attempt ``attempt_count + 1``.

        An explicit ``retry_after`` on a :class:`RetryableError` wins over the
        computed backoff, but is still capped by ``max_backoff_seconds``.
        """
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            return max(0.0, min(float(retry_after), self.max_backoff_seconds))
        exponent = max(0, attempt_count - 1)
        delay = self.backoff_seconds * (self.backoff_multiplier**exponent)
        delay = min(delay, self.max_backoff_seconds)
        if self.jitter:
            spread = delay * self.jitter
            delay = delay + random.uniform(-spread, spread)
        return max(0.0, delay)


#: Applied to any plugin that does not declare its own policy: no retries.
DEFAULT_RETRY_POLICY = RetryPolicy()

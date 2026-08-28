"""Phase 0 reliability: retries, recovery, cancellation, dead letter, retention.

These are the failure-injection tests the reliability milestone requires. Each
one drives a real execution through the engine rather than asserting on helper
functions, because the behaviour under test only exists in the interaction
between claim, execute, transition, and schedule.
"""

import datetime
import threading

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.utils.timezone import now

from djangocms_automation import engine
from djangocms_automation.instances import (
    CANCELED,
    COMPLETED,
    FAILED,
    PENDING,
    RUNNING,
    WAITING,
    AutomationAction,
    AutomationInstance,
    SchedulerLock,
)
from djangocms_automation.models import (
    Automation,
    AutomationContent,
    AutomationTrigger,
    BaseActionPluginModel,
)
from djangocms_automation.retry import PermanentError, RetryableError, RetryPolicy

#: Execution counters keyed by plugin name, reset by the ``counters`` fixture.
CALLS: dict[str, int] = {}

#: Events used to hold an action open while a test inspects it mid-execution.
BARRIER: dict[str, object] = {}


class FlakyActionModel(BaseActionPluginModel):
    """Fails with a retryable error until it has been called three times."""

    retry_policy = RetryPolicy(max_attempts=3, backoff_seconds=0, jitter=0)

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        CALLS["flaky"] = CALLS.get("flaky", 0) + 1
        if CALLS["flaky"] < 3:
            raise RetryableError("transient")
        return [{"ok": True, "calls": CALLS["flaky"]}]


class AlwaysRetryableModel(BaseActionPluginModel):
    """Always fails retryably, so it exhausts its budget."""

    retry_policy = RetryPolicy(max_attempts=2, backoff_seconds=0, jitter=0)

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        CALLS["always"] = CALLS.get("always", 0) + 1
        raise RetryableError("still broken")


class PermanentFailureModel(BaseActionPluginModel):
    """Declares a generous retry budget but raises a permanent error."""

    retry_policy = RetryPolicy(max_attempts=5, backoff_seconds=0, jitter=0)

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        CALLS["permanent"] = CALLS.get("permanent", 0) + 1
        raise PermanentError("misconfigured")


class UnknownFailureModel(BaseActionPluginModel):
    """Raises an unclassified exception: not retried, per the explicit policy."""

    retry_policy = RetryPolicy(max_attempts=5, backoff_seconds=0, jitter=0)

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        CALLS["unknown"] = CALLS.get("unknown", 0) + 1
        raise ValueError("something surprising")


class SlowHeartbeatModel(BaseActionPluginModel):
    """Blocks until the test releases it, so a heartbeat has time to fire."""

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        BARRIER["running"].set()
        BARRIER["release"].wait(timeout=10)
        return rows


class ThreeAttemptModel(BaseActionPluginModel):
    """Declares a retry budget that the action row does not carry by default."""

    retry_policy = RetryPolicy(max_attempts=3, backoff_seconds=0, jitter=0)

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        raise RetryableError("transient")


class SlowActionModel(BaseActionPluginModel):
    """Succeeds, but declares a very short timeout for recovery tests."""

    timeout_seconds = 1

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        return [{"slow": True}]


@plugin_pool.register_plugin
class FlakyPlugin(CMSPluginBase):
    model = FlakyActionModel
    name = "Flaky Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@plugin_pool.register_plugin
class AlwaysRetryablePlugin(CMSPluginBase):
    model = AlwaysRetryableModel
    name = "Always Retryable Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@plugin_pool.register_plugin
class PermanentFailurePlugin(CMSPluginBase):
    model = PermanentFailureModel
    name = "Permanent Failure Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@plugin_pool.register_plugin
class UnknownFailurePlugin(CMSPluginBase):
    model = UnknownFailureModel
    name = "Unknown Failure Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@plugin_pool.register_plugin
class SlowHeartbeatPlugin(CMSPluginBase):
    model = SlowHeartbeatModel
    name = "Slow Heartbeat Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@plugin_pool.register_plugin
class ThreeAttemptPlugin(CMSPluginBase):
    model = ThreeAttemptModel
    name = "Three Attempt Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@plugin_pool.register_plugin
class SlowPlugin(CMSPluginBase):
    model = SlowActionModel
    name = "Slow Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@pytest.fixture(autouse=True)
def counters():
    CALLS.clear()
    yield
    CALLS.clear()


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Reliability", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation, description="Reliability content"
    )


@pytest.fixture
def run_setup(automation_content, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="click", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    return trigger, placeholder


def run(trigger, placeholder, plugin_type, settings):
    """Add a plugin, fire the trigger, and return the resulting action."""
    add_plugin(placeholder=placeholder, plugin_type=plugin_type, language=settings.LANGUAGE_CODE)
    trigger.trigger_execution(data=[{"seed": 1}])
    return AutomationAction.objects.latest("id")


# --------------------------------------------------------------------------
# 0.2 Retry and idempotency policy
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_retryable_failure_reschedules_without_new_action(run_setup, settings):
    """A retryable failure returns to PENDING on the same row, not a new one."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "FlakyPlugin", settings)

    action.refresh_from_db()
    assert action.state == PENDING
    assert action.attempt_count == 1
    assert action.next_attempt_at is not None
    assert AutomationAction.objects.count() == 1  # rescheduled, not duplicated
    assert CALLS["flaky"] == 1


@pytest.mark.django_db
def test_retries_until_success(run_setup, settings):
    """Reviving a rescheduled action re-runs it until it succeeds."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "FlakyPlugin", settings)

    for _ in range(3):
        engine.revive_pending(now() + datetime.timedelta(minutes=5))

    action.refresh_from_db()
    assert action.state == COMPLETED
    assert action.attempt_count == 3
    assert CALLS["flaky"] == 3
    assert action.dead_lettered is False


@pytest.mark.django_db
def test_retry_exhaustion_is_terminal_and_dead_lettered(run_setup, settings):
    """Exhausting the budget fails the action and files it for inspection."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "AlwaysRetryablePlugin", settings)

    for _ in range(4):
        engine.revive_pending(now() + datetime.timedelta(minutes=5))

    action.refresh_from_db()
    assert action.state == FAILED
    assert action.attempt_count == 2  # policy max_attempts
    assert CALLS["always"] == 2
    assert action.dead_lettered is True
    assert action.dead_lettered_at is not None
    assert action.automation_instance.status == FAILED


@pytest.mark.django_db
def test_permanent_error_is_never_retried(run_setup, settings):
    """PermanentError overrides a generous retry policy."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "PermanentFailurePlugin", settings)

    action.refresh_from_db()
    assert action.state == FAILED
    assert action.attempt_count == 1
    assert CALLS["permanent"] == 1


@pytest.mark.django_db
def test_unclassified_exception_is_not_retried(run_setup, settings):
    """An unknown exception fails fast: retrying might repeat a side effect."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "UnknownFailurePlugin", settings)

    action.refresh_from_db()
    assert action.state == FAILED
    assert action.attempt_count == 1
    assert CALLS["unknown"] == 1


def test_backoff_grows_and_is_capped():
    policy = RetryPolicy(backoff_seconds=10, backoff_multiplier=2, max_backoff_seconds=45, jitter=0)
    assert policy.next_delay(1) == 10
    assert policy.next_delay(2) == 20
    assert policy.next_delay(3) == 40
    assert policy.next_delay(4) == 45  # capped


def test_explicit_retry_after_wins_over_backoff():
    policy = RetryPolicy(backoff_seconds=600, max_backoff_seconds=900, jitter=0)
    assert policy.next_delay(1, RetryableError("rate limited", retry_after=12)) == 12


def test_jitter_stays_within_bounds():
    policy = RetryPolicy(backoff_seconds=100, jitter=0.25, backoff_multiplier=1)
    for _ in range(50):
        assert 75 <= policy.next_delay(1) <= 125


@pytest.mark.django_db
def test_duplicate_delivery_cannot_execute_twice(run_setup, settings):
    """A second delivery of the same task is a no-op, not a second execution."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    action.refresh_from_db()
    assert action.state == COMPLETED

    engine.run_action(action.pk)  # redelivery of an already finished action
    action.refresh_from_db()
    assert action.state == COMPLETED
    assert action.attempt_count == 1


# --------------------------------------------------------------------------
# Re-entry is not a retry
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_split_re_entry_does_not_consume_attempts(run_setup, settings):
    """A split re-entering to join counts as a re-entry, never as an attempt."""
    trigger, placeholder = run_setup
    split = add_plugin(placeholder=placeholder, plugin_type="AutomationSplit", language=settings.LANGUAGE_CODE)
    for _ in range(2):
        path = add_plugin(
            placeholder=placeholder,
            plugin_type="AutomationPath",
            language=settings.LANGUAGE_CODE,
            target=split,
        )
        add_plugin(
            placeholder=placeholder,
            plugin_type="ActionPlugin",
            language=settings.LANGUAGE_CODE,
            target=path,
        )
    trigger.trigger_execution(data=[{"seed": 1}])

    split_action = AutomationAction.objects.filter(parent__isnull=True).latest("id")
    split_action.refresh_from_db()
    assert split_action.state == COMPLETED
    assert split_action.re_entry_count >= 1, "the join re-entered the split"
    assert split_action.attempt_count == 1, "re-entry must not consume the retry budget"


@pytest.mark.django_db
def test_pause_does_not_consume_attempts(run_setup, settings):
    """An ActionPause is a deliberate reschedule, not a failed attempt."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    AutomationAction.objects.filter(pk=action.pk).update(state=RUNNING, finished=None)
    AutomationInstance.objects.filter(pk=action.automation_instance_id).update(status=RUNNING, finished=None)
    action.refresh_from_db()

    engine.pause_action(action, until=now() - datetime.timedelta(seconds=1), message="paused")
    action.refresh_from_db()
    assert action.resumed is True

    engine.revive_pending()
    action.refresh_from_db()
    assert action.re_entry_count == 1
    assert action.attempt_count == 1


# --------------------------------------------------------------------------
# 0.3 Recovery, timeouts and cancellation
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_killed_worker_leaves_no_permanently_running_action(run_setup, settings):
    """A RUNNING action whose heartbeat went stale is recovered, not stranded."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "FlakyPlugin", settings)
    stale = now() - datetime.timedelta(hours=1)
    AutomationAction.objects.filter(pk=action.pk).update(
        state=RUNNING, finished=None, started=stale, heartbeat_at=stale, attempt_count=1, max_attempts=3
    )

    assert engine.recover_expired_leases() == 1
    action.refresh_from_db()
    assert action.state == PENDING
    assert "Recovered" in action.message


@pytest.mark.django_db
def test_recovery_fails_action_with_no_attempts_left(run_setup, settings):
    """With the budget spent, recovery is terminal and dead-letters the action."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    stale = now() - datetime.timedelta(hours=1)
    AutomationAction.objects.filter(pk=action.pk).update(
        state=RUNNING, finished=None, started=stale, heartbeat_at=stale, attempt_count=1, max_attempts=1
    )
    AutomationInstance.objects.filter(pk=action.automation_instance_id).update(status=RUNNING, finished=None)

    assert engine.recover_expired_leases() == 1
    action.refresh_from_db()
    assert action.state == FAILED
    assert action.dead_lettered is True
    assert action.automation_instance.status == FAILED


@pytest.mark.django_db
def test_timeout_is_enforced_even_with_a_fresh_heartbeat(run_setup, settings):
    """An action past its own timeout is recovered regardless of heartbeat."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    AutomationAction.objects.filter(pk=action.pk).update(
        state=RUNNING,
        finished=None,
        started=now() - datetime.timedelta(minutes=5),
        heartbeat_at=now(),
        timeout_seconds=1,
        attempt_count=1,
        max_attempts=1,
    )
    AutomationInstance.objects.filter(pk=action.automation_instance_id).update(status=RUNNING, finished=None)

    assert engine.recover_expired_leases() == 1
    action.refresh_from_db()
    assert action.state == FAILED
    assert "timed out" in action.message


@pytest.mark.django_db
def test_healthy_running_action_is_not_recovered(run_setup, settings):
    """Recovery must never touch an action a live worker still owns."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    AutomationAction.objects.filter(pk=action.pk).update(
        state=RUNNING, finished=None, started=now(), heartbeat_at=now()
    )
    assert engine.recover_expired_leases() == 0


@pytest.mark.django_db
def test_cancellation_stops_unfinished_actions(run_setup, settings):
    """Cancelling an instance closes every open action and the instance."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "FlakyPlugin", settings)
    instance = action.automation_instance

    canceled = engine.cancel_instance(instance.pk)
    assert canceled == 1
    action.refresh_from_db()
    instance.refresh_from_db()
    assert action.state == CANCELED
    assert action.finished is not None
    assert instance.status == CANCELED


@pytest.mark.django_db
def test_cancellation_is_idempotent(run_setup, settings):
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "FlakyPlugin", settings)
    instance = action.automation_instance

    assert engine.cancel_instance(instance.pk) == 1
    assert engine.cancel_instance(instance.pk) == 0


@pytest.mark.django_db
def test_canceled_instance_prevents_further_side_effects(run_setup, settings):
    """An action enqueued before cancellation does no work when it lands."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "FlakyPlugin", settings)
    instance = action.automation_instance
    engine.cancel_instance(instance.pk)

    AutomationAction.objects.filter(pk=action.pk).update(state=PENDING, finished=None)
    before = CALLS.get("flaky", 0)
    engine.run_action(action.pk)

    assert CALLS.get("flaky", 0) == before, "a canceled run must not execute"
    action.refresh_from_db()
    assert action.state == CANCELED


@pytest.mark.django_db
def test_revive_skips_canceled_instances(run_setup, settings):
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "FlakyPlugin", settings)
    engine.cancel_instance(action.automation_instance_id)
    AutomationAction.objects.filter(pk=action.pk).update(state=PENDING, finished=None)

    assert engine.revive_pending(now() + datetime.timedelta(minutes=5)) == 0


# --------------------------------------------------------------------------
# Scheduler concurrency
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_second_scheduler_skips_the_tick():
    """Two concurrent schedulers do not both run: one takes the lock."""
    with engine.scheduler_lock("test-lock") as first:
        assert bool(first)
        with engine.scheduler_lock("test-lock") as second:
            assert not bool(second)


@pytest.mark.django_db
def test_lock_is_released_for_the_next_tick():
    with engine.scheduler_lock("test-lock") as first:
        assert bool(first)
    with engine.scheduler_lock("test-lock") as second:
        assert bool(second)


@pytest.mark.django_db
def test_abandoned_lock_expires():
    """A scheduler that died does not block its successor forever."""
    SchedulerLock.acquire("test-lock", ttl_seconds=300)
    SchedulerLock.objects.filter(name="test-lock").update(locked_until=now() - datetime.timedelta(seconds=1))
    assert SchedulerLock.acquire("test-lock") is not None


@pytest.mark.django_db
def test_management_command_respects_the_lock(run_setup, settings, capsys):
    SchedulerLock.acquire("runautomations", ttl_seconds=300)
    call_command("runautomations")
    assert "Another scheduler holds the lock" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 0.4 Dead letter and replay
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_replay_creates_a_new_action_without_mutating_history(run_setup, settings):
    """Replay is auditable: the original row is untouched and linked from the new one."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "UnknownFailurePlugin", settings)
    action.refresh_from_db()
    assert action.dead_lettered is True
    original_finished = action.finished

    replacement = engine.replay_action(action.pk)
    assert replacement is not None
    assert replacement.pk != action.pk
    assert replacement.replayed_from_id == action.pk
    assert replacement.plugin_ptr == action.plugin_ptr

    action.refresh_from_db()
    assert action.state == FAILED
    assert action.finished == original_finished
    assert CALLS["unknown"] == 2, "the replay actually re-ran the action"


@pytest.mark.django_db
def test_replay_uses_the_input_the_failed_attempt_saw(run_setup, settings):
    """The stored input, not current instance data, seeds the replay."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "UnknownFailurePlugin", settings)
    action.refresh_from_db()
    assert action.input_data == [{"seed": 1}]

    AutomationInstance.objects.filter(pk=action.automation_instance_id).update(data=[{"seed": 999}])
    replacement = engine.replay_action(action.pk)
    replacement.refresh_from_db()
    assert replacement.input_data == [{"seed": 1}]


@pytest.mark.django_db
def test_replay_reopens_the_instance(run_setup, settings):
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "UnknownFailurePlugin", settings)
    assert AutomationInstance.objects.get(pk=action.automation_instance_id).status == FAILED

    engine.replay_action(action.pk)
    instance = AutomationInstance.objects.get(pk=action.automation_instance_id)
    assert instance.status == FAILED  # it failed again, but it did re-run
    assert action.replays.count() == 1


@pytest.mark.django_db
def test_replay_refuses_an_unfinished_action(run_setup, settings):
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "FlakyPlugin", settings)
    action.refresh_from_db()
    assert action.state == PENDING
    assert engine.replay_action(action.pk) is None


# --------------------------------------------------------------------------
# 0.6 Observability and retention
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_transitions_emit_a_signal(run_setup, settings):
    """The signal is the supported hook for metrics and tracing."""
    from djangocms_automation.signals import action_transitioned

    seen = []

    def receiver(sender, **kwargs):
        seen.append((kwargs["from_state"], kwargs["to_state"]))

    action_transitioned.connect(receiver)
    try:
        run(trigger=run_setup[0], placeholder=run_setup[1], plugin_type="SlowPlugin", settings=settings)
    finally:
        action_transitioned.disconnect(receiver)

    assert (PENDING, RUNNING) in seen
    assert (RUNNING, COMPLETED) in seen


@pytest.mark.django_db
def test_a_broken_receiver_cannot_fail_an_execution(run_setup, settings):
    """Observability must never take down a run."""
    from djangocms_automation.signals import action_transitioned

    def broken(sender, **kwargs):
        raise RuntimeError("metrics backend down")

    action_transitioned.connect(broken)
    try:
        action = run(run_setup[0], run_setup[1], "SlowPlugin", settings)
    finally:
        action_transitioned.disconnect(broken)

    action.refresh_from_db()
    assert action.state == COMPLETED


@pytest.mark.django_db
def test_one_execution_can_be_followed_by_its_events(run_setup, settings):
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    events = list(action.events.order_by("created").values_list("from_state", "to_state"))
    assert (PENDING, RUNNING) in events
    assert (RUNNING, COMPLETED) in events


@pytest.mark.django_db
def test_redaction_drops_payloads_but_keeps_metadata(run_setup, settings):
    """Retention can strip data while leaving the audit trail intact."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    instance = action.automation_instance
    old = now() - datetime.timedelta(days=90)
    AutomationInstance.objects.filter(pk=instance.pk).update(finished=old, updated=old)

    assert AutomationInstance.redact_payloads(days=30) == 1
    instance.refresh_from_db()
    action.refresh_from_db()
    assert instance.data == [] and instance.initial_data == []
    assert action.input_data is None
    assert action.state == COMPLETED, "state and timings survive redaction"
    assert action.events.exists()


@pytest.mark.django_db
def test_retention_never_removes_an_active_execution(run_setup, settings):
    """An unfinished run is not deleted however old it is."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "FlakyPlugin", settings)
    old = now() - datetime.timedelta(days=365)
    AutomationInstance.objects.filter(pk=action.automation_instance_id).update(updated=old)

    AutomationInstance.delete_history(days=30)
    assert AutomationInstance.objects.filter(pk=action.automation_instance_id).exists()


@pytest.mark.django_db
def test_cleanup_flag_deletes_old_history(run_setup, settings):
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    old = now() - datetime.timedelta(days=90)
    AutomationInstance.objects.filter(pk=action.automation_instance_id).update(finished=old, updated=old)

    call_command("runautomations", "--cleanup", "30", "--no-lock")
    assert not AutomationInstance.objects.filter(pk=action.automation_instance_id).exists()


# --------------------------------------------------------------------------
# Timer catch-up
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_catch_up_fires_several_missed_occurrences(run_setup, settings):
    """A configurable limit drains a backlog faster than one per tick."""
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)
    trigger.type = "timer"
    trigger.config = {
        "scheduled_at": (now() - datetime.timedelta(hours=3)).isoformat(),
        "recurrence_frequency": "hourly",
        "recurrence_interval": 1,
    }
    trigger.save()

    assert engine.fire_due_timers(catch_up=4) == 4
    trigger.refresh_from_db()
    assert trigger.config["fired_count"] == 4


@pytest.mark.django_db
def test_catch_up_limit_prevents_a_restart_storm(run_setup, settings):
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)
    trigger.type = "timer"
    trigger.config = {
        "scheduled_at": (now() - datetime.timedelta(hours=50)).isoformat(),
        "recurrence_frequency": "hourly",
        "recurrence_interval": 1,
    }
    trigger.save()

    assert engine.fire_due_timers(catch_up=3) == 3


@pytest.mark.django_db
def test_stale_occurrences_are_skipped_not_fired(run_setup, settings):
    """With a max age set, ancient occurrences are dropped rather than replayed."""
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)
    settings.AUTOMATION_TIMER_MAX_AGE = 3600  # one hour
    trigger.type = "timer"
    trigger.config = {
        "scheduled_at": (now() - datetime.timedelta(hours=10)).isoformat(),
        "recurrence_frequency": "hourly",
        "recurrence_interval": 1,
    }
    trigger.save()

    fired = engine.fire_due_timers(catch_up=2)
    trigger.refresh_from_db()
    assert fired <= 2
    assert trigger.config["fired_count"] <= 2


# --------------------------------------------------------------------------
# Enqueue rejection
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_enqueue_rejection_is_persisted_and_replayable(run_setup, settings, monkeypatch):
    """A broker outage is recorded on the action, not lost."""
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="SlowPlugin", language=settings.LANGUAGE_CODE)

    from djangocms_automation import tasks

    class RejectingTask:
        def enqueue(self, *args, **kwargs):
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(tasks, "execute_action", RejectingTask())
    trigger.trigger_execution(data=[{"seed": 1}])

    action = AutomationAction.objects.latest("id")
    assert action.state == FAILED
    assert action.result["error"] == "Task enqueue failed"
    assert action.dead_lettered is True


@pytest.mark.django_db
def test_empty_trigger_placeholder_reports_a_usable_error(automation_content, settings):
    """A misconfigured automation must not raise AttributeError from deep inside."""
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="empty", type="click", position=0
    )
    Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="empty",
    )

    with pytest.raises(ValueError, match="no plugins to execute"):
        trigger.trigger_execution(data=[{"seed": 1}])

    assert AutomationInstance.objects.count() == 0


@pytest.mark.django_db
def test_lost_join_wakeup_is_reconciled(run_setup, settings):
    """A crash between a child finishing and waking its parent must not strand the join."""
    trigger, placeholder = run_setup
    split = add_plugin(placeholder=placeholder, plugin_type="AutomationSplit", language=settings.LANGUAGE_CODE)
    path = add_plugin(
        placeholder=placeholder, plugin_type="AutomationPath", language=settings.LANGUAGE_CODE, target=split
    )
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, target=path)
    trigger.trigger_execution(data=[{"seed": 1}])

    split_action = AutomationAction.objects.filter(parent__isnull=True).latest("id")
    # Simulate the lost wake-up: children finished, parent left WAITING.
    AutomationAction.objects.filter(pk=split_action.pk).update(state=WAITING, finished=None)
    AutomationInstance.objects.filter(pk=split_action.automation_instance_id).update(status=RUNNING, finished=None)

    assert engine.reconcile_waiting_joins() == 1
    split_action.refresh_from_db()
    assert split_action.state == COMPLETED


@pytest.mark.django_db
def test_reconciliation_leaves_genuinely_waiting_joins_alone(run_setup, settings):
    """A join with an unfinished child must not be woken early."""
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)
    trigger.trigger_execution(data=[{"seed": 1}])
    parent = AutomationAction.objects.latest("id")
    AutomationAction.objects.filter(pk=parent.pk).update(state=WAITING, finished=None)
    AutomationAction.objects.create(
        parent=parent,
        automation_instance=parent.automation_instance,
        plugin_ptr=parent.plugin_ptr,
        finished=None,
    )

    assert engine.reconcile_waiting_joins() == 0


# --------------------------------------------------------------------------
# Coverage gaps: engine guards, retention flag, misconfiguration
# --------------------------------------------------------------------------


def test_should_retry_honours_a_per_action_limit():
    """The engine resolves an effective limit; the policy must accept it."""
    policy = RetryPolicy(max_attempts=2, backoff_seconds=0, jitter=0)
    exc = RetryableError("transient")

    assert policy.should_retry(exc, attempt_count=1) is True
    assert policy.should_retry(exc, attempt_count=2) is False
    assert policy.should_retry(exc, attempt_count=2, max_attempts=5) is True
    assert policy.should_retry(PermanentError("no"), attempt_count=1, max_attempts=5) is False


@pytest.mark.django_db
def test_lost_wakeup_guard_wakes_a_parent_whose_children_all_finished(run_setup, settings):
    """``_wake_if_children_done`` closes the window where every child finished
    while the parent was still RUNNING, so no child found a WAITING row to wake."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    AutomationAction.objects.filter(pk=action.pk).update(state=WAITING, finished=None)
    AutomationInstance.objects.filter(pk=action.automation_instance_id).update(status=RUNNING, finished=None)
    AutomationAction.objects.create(
        parent_id=action.pk,
        automation_instance=action.automation_instance,
        plugin_ptr=action.plugin_ptr,
        state=COMPLETED,
        finished=now(),
    )
    action.refresh_from_db()

    engine._wake_if_children_done(action)

    action.refresh_from_db()
    assert action.state == COMPLETED, "the parent resumed and ran to completion"


@pytest.mark.django_db
def test_a_broken_instance_receiver_cannot_fail_a_run(run_setup, settings):
    """As for action transitions, instance observability must never break a run."""
    from djangocms_automation.signals import instance_finished

    def broken(sender, **kwargs):
        raise RuntimeError("metrics backend down")

    instance_finished.connect(broken)
    try:
        action = run(trigger=run_setup[0], placeholder=run_setup[1], plugin_type="SlowPlugin", settings=settings)
    finally:
        instance_finished.disconnect(broken)

    action.refresh_from_db()
    assert action.state == COMPLETED
    assert action.automation_instance.status == COMPLETED


@pytest.mark.django_db
def test_redact_flag_strips_payloads_but_keeps_the_run(run_setup, settings):
    """``--redact`` is the retention level that keeps the audit trail."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    old = now() - datetime.timedelta(days=90)
    AutomationInstance.objects.filter(pk=action.automation_instance_id).update(finished=old, updated=old)

    call_command("runautomations", "--redact", "30", "--no-lock")

    instance = AutomationInstance.objects.get(pk=action.automation_instance_id)
    action.refresh_from_db()
    assert instance.data == [] and instance.initial_data == []
    assert action.input_data is None
    assert action.state == COMPLETED
    assert action.events.exists(), "the audit trail survives redaction"


@pytest.mark.django_db
def test_trigger_without_a_placeholder_reports_a_usable_error(automation_content, settings):
    """A trigger pointing at a slot that has no placeholder at all."""
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="nonexistent", type="click", position=0
    )

    with pytest.raises(ValueError, match="no placeholder"):
        trigger.trigger_execution(data=[{"seed": 1}])

    assert AutomationInstance.objects.count() == 0


# --------------------------------------------------------------------------
# Lease renewal: a long action must not look abandoned
# --------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_heartbeat_is_renewed_while_an_action_runs(run_setup, settings):
    """A legitimately long action must keep its lease alive.

    Without renewal, an action running past ``AUTOMATION_LEASE_SECONDS`` looks
    abandoned to the scheduler, which recovers it and runs it a second time —
    the duplicate side effect leases exist to prevent.
    """
    settings.AUTOMATION_LEASE_SECONDS = 3  # refresh interval becomes 1s
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="SlowHeartbeatPlugin", language=settings.LANGUAGE_CODE)

    BARRIER["running"] = threading.Event()
    BARRIER["release"] = threading.Event()
    errors = []

    def fire():
        try:
            trigger.trigger_execution(data=[{"seed": 1}])
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    worker = threading.Thread(target=fire)
    worker.start()
    try:
        assert BARRIER["running"].wait(timeout=10), "the action never started"
        action = AutomationAction.objects.latest("id")
        first = AutomationAction.objects.get(pk=action.pk).heartbeat_at

        deadline = now() + datetime.timedelta(seconds=8)
        while now() < deadline:
            if AutomationAction.objects.get(pk=action.pk).heartbeat_at > first:
                break
            threading.Event().wait(0.25)
        else:  # pragma: no cover - only reached on failure
            pytest.fail("heartbeat was never refreshed while the action ran")
    finally:
        BARRIER["release"].set()
        worker.join(timeout=15)

    assert not errors, errors


@pytest.mark.django_db(transaction=True)
def test_heartbeat_stops_when_the_action_finishes(run_setup, settings):
    """The refresher must not outlive the action it was renewing."""
    settings.AUTOMATION_LEASE_SECONDS = 3
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="SlowHeartbeatPlugin", language=settings.LANGUAGE_CODE)

    BARRIER["running"] = threading.Event()
    BARRIER["release"] = threading.Event()
    BARRIER["release"].set()  # do not block
    trigger.trigger_execution(data=[{"seed": 1}])

    before = threading.active_count()
    threading.Event().wait(0.5)
    assert threading.active_count() <= before, "the heartbeat thread outlived the action"
    assert AutomationAction.objects.latest("id").state == COMPLETED


@pytest.mark.django_db
def test_recovery_honours_the_plugins_retry_budget(run_setup, settings):
    """A crashed worker must not dead-letter an action that had attempts left.

    Recovery runs in the scheduler, which has no plugin instance to ask, so the
    budget is persisted onto the action when it is claimed.
    """
    trigger, placeholder = run_setup
    add_plugin(placeholder=placeholder, plugin_type="ThreeAttemptPlugin", language=settings.LANGUAGE_CODE)
    trigger.trigger_execution(data=[{"seed": 1}])

    action = AutomationAction.objects.latest("id")
    assert action.max_attempts == 3, "the plugin's budget must be persisted at claim time"

    stale = now() - datetime.timedelta(hours=1)
    AutomationAction.objects.filter(pk=action.pk).update(
        state=RUNNING, finished=None, started=stale, heartbeat_at=stale, attempt_count=1
    )
    AutomationInstance.objects.filter(pk=action.automation_instance_id).update(status=RUNNING, finished=None)

    assert engine.recover_expired_leases() == 1
    action.refresh_from_db()
    assert action.state == PENDING, "attempts remained, so it must be retried"
    assert action.dead_lettered is False


# --------------------------------------------------------------------------
# Replaying a branch: superseded siblings and reopened ancestors
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_replaying_a_branch_action_lets_the_split_join(run_setup, settings):
    """Replaying a failed branch must let the run finish.

    Two things have to hold. The split's failed ancestors are reopened so the
    join can be woken, and the superseded original is excluded from the join —
    otherwise the split re-runs, sees its old failed child, and fails again.
    """
    trigger, placeholder = run_setup
    split = add_plugin(placeholder=placeholder, plugin_type="AutomationSplit", language=settings.LANGUAGE_CODE)
    path = add_plugin(
        placeholder=placeholder, plugin_type="AutomationPath", language=settings.LANGUAGE_CODE, target=split
    )
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, target=path)
    trigger.trigger_execution(data=[{"seed": 1}])

    child = AutomationAction.objects.filter(parent__isnull=False).latest("id")
    parent = child.parent
    # Stage the fail-fast outcome: child failed, ancestors and instance closed.
    AutomationAction.objects.filter(pk=child.pk).update(
        state=FAILED, finished=now(), dead_lettered=True, dead_lettered_at=now(), input_data=[{"seed": 1}]
    )
    AutomationAction.objects.filter(pk=parent.pk).update(state=FAILED, finished=now())
    AutomationInstance.objects.filter(pk=child.automation_instance_id).update(status=FAILED, finished=now())

    replacement = engine.replay_action(child.pk)
    assert replacement is not None

    parent.refresh_from_db()
    replacement.refresh_from_db()
    child.refresh_from_db()
    instance = AutomationInstance.objects.get(pk=child.automation_instance_id)
    assert replacement.state == COMPLETED
    assert parent.state == COMPLETED, "the reopened split must join, not stall"
    assert instance.status == COMPLETED, "the instance must not run forever"
    assert child.state == FAILED, "the superseded original stays as history"


@pytest.mark.django_db
def test_replaying_a_top_level_action_has_no_ancestors_to_reopen(run_setup, settings):
    """The common case must not be disturbed by the ancestor walk."""
    trigger, placeholder = run_setup
    action = run(trigger, placeholder, "SlowPlugin", settings)
    AutomationAction.objects.filter(pk=action.pk).update(
        state=FAILED, finished=now(), dead_lettered=True, input_data=[]
    )

    replacement = engine.replay_action(action.pk)
    assert replacement is not None
    assert replacement.parent_id is None


@pytest.mark.django_db
def test_a_broken_dead_letter_receiver_does_not_block_propagation(run_setup, settings):
    """A raising receiver must not leave the failure un-propagated."""
    from djangocms_automation.signals import action_dead_lettered

    trigger, placeholder = run_setup

    def broken(sender, **kwargs):
        raise RuntimeError("alerting backend down")

    action_dead_lettered.connect(broken)
    try:
        action = run(trigger, placeholder, "UnknownFailurePlugin", settings)
    finally:
        action_dead_lettered.disconnect(broken)

    action.refresh_from_db()
    assert action.state == FAILED
    assert action.dead_lettered is True
    assert action.automation_instance.status == FAILED, "failure must still have propagated"

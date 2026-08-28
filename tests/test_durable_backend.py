"""Integration tests against a real, non-immediate task backend.

Every other test runs on ``ImmediateBackend``, which executes inline and hides
the class of bug that only appears when enqueue and execution are separated in
time and process: work that vanishes on rollback, work executed before its
transaction commits, work stranded by a dead worker. These tests use the durable
database-backed queue and a worker draining it explicitly.
"""

import datetime

import pytest

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import transaction
from django.utils.timezone import now

from cms.api import add_plugin
from cms.models import Placeholder
from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool

from djangocms_automation.instances import (
    COMPLETED,
    AutomationAction,
    AutomationInstance,
)
from djangocms_automation.models import (
    Automation,
    AutomationContent,
    AutomationTrigger,
    BaseActionPluginModel,
)
from djangocms_automation.queue import READY, RUNNING, SUCCESSFUL, QueuedTask

#: Filled in by ``AtomicProbeModel`` while it executes.
PROBE: dict = {}


class AtomicProbeModel(BaseActionPluginModel):
    """Records whether the worker executed it inside a transaction."""

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        from django.db import connection

        PROBE["in_atomic_block"] = connection.in_atomic_block
        return rows


@plugin_pool.register_plugin
class AtomicProbePlugin(CMSPluginBase):
    model = AtomicProbeModel
    name = "Atomic Probe Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@pytest.fixture
def durable(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "djangocms_automation.backends.DatabaseBackend",
            "QUEUES": ["default"],
        }
    }
    return settings


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Durable", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Durable content")


@pytest.fixture
def setup(automation_content, durable):
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="click", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=durable.LANGUAGE_CODE)
    return trigger, placeholder


@pytest.mark.django_db(transaction=True)
def test_enqueue_persists_and_does_not_execute_inline(setup):
    """The defining property of a durable backend: enqueue is not execution."""
    trigger, _placeholder = setup
    trigger.trigger_execution(data=[{"seed": 1}])

    assert QueuedTask.objects.filter(state=READY).count() == 1
    action = AutomationAction.objects.latest("id")
    assert action.state != COMPLETED, "nothing should have run yet"


@pytest.mark.django_db(transaction=True)
def test_worker_drains_the_queue_and_completes_the_run(setup):
    trigger, _placeholder = setup
    trigger.trigger_execution(data=[{"seed": 1}])

    call_command("runworker", "--once")

    action = AutomationAction.objects.latest("id")
    action.refresh_from_db()
    assert action.state == COMPLETED
    assert QueuedTask.objects.filter(state=SUCCESSFUL).count() == 1
    assert AutomationInstance.objects.latest("id").status == COMPLETED


@pytest.mark.django_db(transaction=True)
def test_rolled_back_transaction_enqueues_nothing(setup):
    """Work must not become visible if the transaction that created it aborts."""
    trigger, _placeholder = setup

    class Rollback(Exception):
        pass

    with pytest.raises(Rollback):
        with transaction.atomic():
            trigger.trigger_execution(data=[{"seed": 1}])
            raise Rollback

    assert QueuedTask.objects.count() == 0
    assert AutomationInstance.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_a_killed_worker_releases_its_task(setup):
    """A claimed task whose lease expired returns to the queue for another worker."""
    trigger, _placeholder = setup
    trigger.trigger_execution(data=[{"seed": 1}])

    task_row = QueuedTask.objects.get()
    QueuedTask.objects.filter(pk=task_row.pk).update(
        state=RUNNING,
        worker_id="dead-worker",
        claimed_until=now() - datetime.timedelta(minutes=1),
    )

    assert QueuedTask.release_expired() == 1
    task_row.refresh_from_db()
    assert task_row.state == READY
    assert task_row.worker_id == ""

    call_command("runworker", "--once")
    assert AutomationAction.objects.latest("id").state == COMPLETED


@pytest.mark.django_db(transaction=True)
def test_a_live_claim_is_not_stolen(setup):
    """Release must only reclaim expired leases, never a task in progress."""
    trigger, _placeholder = setup
    trigger.trigger_execution(data=[{"seed": 1}])
    QueuedTask.objects.update(state=RUNNING, worker_id="busy", claimed_until=now() + datetime.timedelta(minutes=5))
    assert QueuedTask.release_expired() == 0


@pytest.mark.django_db(transaction=True)
def test_two_workers_do_not_execute_the_same_task(setup):
    """Claiming is exclusive: the second worker finds nothing to do."""
    trigger, _placeholder = setup
    trigger.trigger_execution(data=[{"seed": 1}])

    first = QueuedTask.claim_next(["default"], "worker-a")
    assert first is not None
    second = QueuedTask.claim_next(["default"], "worker-b")
    assert second is None


@pytest.mark.django_db(transaction=True)
def test_deferred_tasks_do_not_run_before_they_are_due(setup):
    trigger, _placeholder = setup
    trigger.trigger_execution(data=[{"seed": 1}])
    QueuedTask.objects.update(run_after=now() + datetime.timedelta(hours=1))

    assert QueuedTask.claim_next(["default"], "worker-a") is None


@pytest.mark.django_db(transaction=True)
def test_finished_task_rows_can_be_purged(setup):
    trigger, _placeholder = setup
    trigger.trigger_execution(data=[{"seed": 1}])
    call_command("runworker", "--once")

    QueuedTask.objects.update(finished_at=now() - datetime.timedelta(days=30))
    assert QueuedTask.purge(days=7) == 1
    assert QueuedTask.objects.count() == 0


# --------------------------------------------------------------------------
# Reference automations
#
# The demo project seeds these as real plugin trees (``seedautomations``);
# these tests assert the same shapes end to end against the durable backend, so
# a reference automation cannot silently stop working.
# --------------------------------------------------------------------------


@pytest.fixture
def reference_setup(automation_content, durable):
    durable.AUTOMATION_ALLOWED_MODELS = ["auth.User"]
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="webhook", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    return trigger, placeholder


@pytest.mark.django_db(transaction=True)
def test_reference_webhook_ingest_runs_end_to_end(reference_setup, durable, mailoutbox):
    """Webhook → Create Record → Send Email, drained by a worker."""
    trigger, placeholder = reference_setup
    add_plugin(
        placeholder=placeholder,
        plugin_type="CreateModelAction",
        language=durable.LANGUAGE_CODE,
        config={
            "model": "auth.User",
            "field_mapping": {"username": "username", "email": "email"},
        },
    )
    add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=durable.LANGUAGE_CODE,
        config={
            "subject": '"Welcome"',
            "body": "Hello {{ username }}",
            "recipient_email": "email",
        },
    )

    from django.contrib.auth import get_user_model

    trigger.trigger_execution(
        data=[{"username": "ingested", "email": "ingested@example.com"}],
        idempotency_key="ORDER-1",
    )
    call_command("runworker", "--once")

    assert get_user_model().objects.filter(username="ingested").exists()
    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == ["ingested@example.com"]
    assert AutomationInstance.objects.latest("id").status == COMPLETED


@pytest.mark.django_db(transaction=True)
def test_reference_webhook_ingest_is_idempotent(reference_setup, durable):
    """A redelivered webhook creates one execution, not two."""
    trigger, placeholder = reference_setup
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=durable.LANGUAGE_CODE)

    for _ in range(2):
        trigger.trigger_execution(data=[{"reference": "R-1"}], idempotency_key="R-1")

    assert AutomationInstance.objects.filter(idempotency_key="R-1").count() == 1


@pytest.mark.django_db(transaction=True)
def test_reference_digest_queries_then_mails(automation_content, durable, mailoutbox):
    """Timer → Query Records → Send Email, the nightly digest shape."""
    durable.AUTOMATION_ALLOWED_MODELS = ["auth.User"]
    from django.contrib.auth import get_user_model

    get_user_model().objects.create(username="reader", email="reader@example.com")

    AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="start",
        type="timer",
        position=0,
        config={"scheduled_at": (now() - datetime.timedelta(minutes=1)).isoformat()},
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    add_plugin(
        placeholder=placeholder,
        plugin_type="QueryModelAction",
        language=durable.LANGUAGE_CODE,
        config={
            "model": "auth.User",
            "filters": {"username": '"reader"'},
            "fields": "username,email",
            "limit": 10,
        },
    )
    add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=durable.LANGUAGE_CODE,
        config={
            "subject": '"Digest"',
            "body": "Latest: {{ username }}",
            "recipient_email": '"editors@example.com"',
        },
    )

    from djangocms_automation import engine

    assert engine.fire_due_timers() == 1
    call_command("runworker", "--once")

    assert len(mailoutbox) == 1
    assert "reader" in mailoutbox[0].body
    assert AutomationInstance.objects.latest("id").status == COMPLETED


# --------------------------------------------------------------------------
# Worker robustness
# --------------------------------------------------------------------------


def queue_a_task(task_path="djangocms_automation.tasks.execute_action", **kwargs):
    """Put a raw row on the queue, bypassing the engine."""
    from django.utils.crypto import get_random_string

    return QueuedTask.objects.create(
        result_id=get_random_string(32),
        task_path=task_path,
        queue_name="default",
        args=kwargs.pop("args", []),
        kwargs=kwargs.pop("kwargs", {}),
        **kwargs,
    )


@pytest.mark.django_db(transaction=True)
def test_a_failing_task_is_recorded_and_does_not_kill_the_worker(setup):
    """One bad task must not take the worker down, and must leave a trace."""
    queue_a_task(task_path="djangocms_automation.does_not_exist.nope")
    trigger, _placeholder = setup
    trigger.trigger_execution(data=[{"seed": 1}])  # a good task behind the bad one

    call_command("runworker", "--once")

    bad = QueuedTask.objects.get(task_path__endswith="nope")
    assert bad.state == "FAILED"
    assert bad.error, "the traceback must be persisted for inspection"
    assert bad.finished_at is not None

    good = QueuedTask.objects.exclude(pk=bad.pk).filter(state=SUCCESSFUL)
    assert good.exists(), "the worker carried on after the failure"
    assert AutomationAction.objects.latest("id").state == COMPLETED


@pytest.mark.django_db(transaction=True)
def test_max_tasks_stops_the_worker(setup):
    """``--max-tasks`` lets a deployment recycle long-lived worker processes."""
    for _ in range(3):
        queue_a_task(task_path="djangocms_automation.does_not_exist.nope")

    call_command("runworker", "--max-tasks", "1")

    assert QueuedTask.objects.filter(state=READY).count() == 2
    assert QueuedTask.objects.filter(state="FAILED").count() == 1


@pytest.mark.django_db(transaction=True)
def test_worker_installs_a_graceful_shutdown_handler(setup):
    """SIGINT/SIGTERM must ask the loop to stop rather than kill it mid-task."""
    import signal

    original = signal.getsignal(signal.SIGTERM)
    try:
        call_command("runworker", "--once")
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        assert handler is not original, "the worker installed its own handler"

        # The handler is idempotent: a second signal must not raise or re-announce.
        handler(signal.SIGTERM, None)
        handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, original)


@pytest.mark.django_db(transaction=True)
def test_worker_idles_when_the_queue_is_empty(setup):
    """With nothing ready, ``--once`` exits instead of spinning."""
    assert QueuedTask.objects.count() == 0
    call_command("runworker", "--once", "--sleep", "0")
    assert QueuedTask.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_the_worker_does_not_wrap_a_task_in_a_transaction(automation_content, durable):
    """Plugin execution must not run inside one long transaction.

    The engine commits the claim, heartbeats and state changes as it goes. A
    transaction around the whole task would hide every one of them until it
    returned, and its row locks would block cancellation and lease recovery — so
    a hung action could never be timed out or recovered.
    """
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="click", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    add_plugin(placeholder=placeholder, plugin_type="AtomicProbePlugin", language=durable.LANGUAGE_CODE)

    PROBE.clear()
    trigger.trigger_execution(data=[{"seed": 1}])
    call_command("runworker", "--once")

    assert PROBE.get("in_atomic_block") is False, "the worker held a transaction around the task"
    assert AutomationAction.objects.latest("id").state == COMPLETED

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

from djangocms_automation.instances import (
    COMPLETED,
    AutomationAction,
    AutomationInstance,
)
from djangocms_automation.models import (
    Automation,
    AutomationContent,
    AutomationTrigger,
)
from djangocms_automation.queue import READY, RUNNING, SUCCESSFUL, QueuedTask


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

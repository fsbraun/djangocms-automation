"""Tests for the threaded task backend (utils.ThreadBackend)."""

import threading
import time

import pytest
from django.tasks import TaskResultStatus, task
from django.tasks.exceptions import InvalidTask, TaskResultDoesNotExist

from djangocms_automation.utils import ThreadBackend


@task
def add(a, b):
    return a + b


@task
def fail():
    raise RuntimeError("kapow")


@task
def thread_identity():
    return threading.get_ident()


@task(takes_context=True)
def context_result_id(context):
    return context.task_result.id


@pytest.fixture
def backend():
    instance = ThreadBackend("threaded", {"QUEUES": [], "OPTIONS": {"MAX_WORKERS": 2}})
    try:
        yield instance
    finally:
        instance._executor.shutdown(wait=True)


def _wait_for(backend, task_result, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task_result = backend.get_result(task_result.id)
        if task_result.is_finished:
            return task_result
        time.sleep(0.01)
    raise AssertionError("Task did not finish in time")


def test_enqueue_executes_real_django_task_and_stores_result(backend):
    caller_thread = threading.get_ident()

    result = backend.enqueue(add, (2, 3), {})
    assert _wait_for(backend, result).return_value == 5
    assert result.status == TaskResultStatus.SUCCESSFUL

    identity_result = backend.enqueue(thread_identity, (), {})
    assert _wait_for(backend, identity_result).return_value != caller_thread


def test_enqueue_records_failure_as_task_error(backend):
    result = _wait_for(backend, backend.enqueue(fail, (), {}))

    assert result.status == TaskResultStatus.FAILED
    assert result.finished_at is not None
    assert result.errors[0].exception_class_path == "builtins.RuntimeError"
    assert "kapow" in result.errors[0].traceback
    with pytest.raises(ValueError, match="Task failed"):
        result.return_value


def test_task_context_receives_its_result(backend):
    result = backend.enqueue(context_result_id, (), {})
    assert _wait_for(backend, result).return_value == result.id


def test_submission_failure_is_propagated_and_result_removed(backend, monkeypatch):
    def reject(*args, **kwargs):
        raise RuntimeError("executor stopped")

    monkeypatch.setattr(backend._executor, "submit", reject)
    with pytest.raises(RuntimeError, match="executor stopped"):
        backend.enqueue(add, (2, 3), {})
    assert backend._results == {}


def test_get_result_unknown_id(backend):
    with pytest.raises(TaskResultDoesNotExist):
        backend.get_result("nope")


def test_enqueue_validates_task(backend):
    invalid = add.using(priority=1)
    with pytest.raises(InvalidTask, match="priority"):
        backend.enqueue(invalid, (2, 3), {})


def test_invalid_worker_count():
    with pytest.raises(ValueError, match="MAX_WORKERS"):
        ThreadBackend("threaded", {"OPTIONS": {"MAX_WORKERS": 0}})

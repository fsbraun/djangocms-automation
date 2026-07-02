"""Tests for the threaded task backend (utils.ThreadBackend)."""

import time

import pytest

from djangocms_automation.utils import ThreadBackend


@pytest.fixture
def backend():
    return ThreadBackend("default", {"QUEUES": [], "OPTIONS": {}})


def _wait_for(backend, task_id, timeout=5.0):
    """Poll get_result until the task leaves the pending state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = backend.get_result(task_id)
        except Exception:
            raise
        if result is not None:
            return result
        # None means still pending — but a completed task may legitimately
        # return None; distinguish via the internal state.
        with backend._lock:
            if backend._results[task_id]["status"] != "pending":
                return backend._results[task_id].get("result")
        time.sleep(0.01)
    raise AssertionError("Task did not finish in time")


@pytest.mark.django_db
def test_enqueue_executes_task_and_stores_result(backend):
    task_id = backend.enqueue(lambda a, b: a + b, (2, 3), {})
    assert _wait_for(backend, task_id) == 5


@pytest.mark.django_db
def test_enqueue_records_failure(backend):
    def boom():
        raise RuntimeError("kapow")

    task_id = backend.enqueue(boom, (), {})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with backend._lock:
            status = backend._results[task_id]["status"]
        if status != "pending":
            break
        time.sleep(0.01)
    assert status == "failed"
    with pytest.raises(Exception, match="kapow"):
        backend.get_result(task_id)


def test_get_result_unknown_id(backend):
    with pytest.raises(ValueError, match="No task found"):
        backend.get_result("nope")

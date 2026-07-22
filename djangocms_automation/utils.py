import threading
from concurrent.futures import ThreadPoolExecutor
from traceback import format_exception

from django.db import close_old_connections, connections
from django.tasks.base import TaskContext, TaskError, TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.exceptions import TaskResultDoesNotExist
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.json import normalize_json


class ThreadBackend(BaseTaskBackend):
    """Run Django tasks in a bounded pool of in-process worker threads.

    This backend is intended for development and other non-durable workloads.
    Enqueued tasks and their results are lost when the process exits and are
    not visible to other application processes.
    """

    supports_get_result = True

    def __init__(self, alias, params):
        super().__init__(alias, params)
        max_workers = int(self.options.get("MAX_WORKERS", 4))
        if max_workers < 1:
            raise ValueError("ThreadBackend MAX_WORKERS must be at least 1")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"django-tasks-{alias}")
        self._results = {}
        self._lock = threading.Lock()
        self.worker_id = get_random_string(32)

    def enqueue(self, task, args, kwargs):
        """Validate and submit a task, returning Django's ``TaskResult``."""
        self.validate_task(task)
        task_result = TaskResult(
            task=task,
            id=get_random_string(32),
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            started_at=None,
            last_attempted_at=None,
            finished_at=None,
            args=args,
            kwargs=kwargs,
            backend=self.alias,
            errors=[],
            worker_ids=[],
        )
        with self._lock:
            self._results[task_result.id] = task_result

        task_enqueued.send(type(self), task_result=task_result)
        try:
            self._executor.submit(self._execute_task, task_result)
        except BaseException:
            # Submission failed, so callers must be able to handle the enqueue
            # failure themselves rather than seeing a permanently READY result.
            with self._lock:
                self._results.pop(task_result.id, None)
            raise
        return task_result

    def _execute_task(self, task_result):
        close_old_connections()
        started_at = timezone.now()
        with self._lock:
            object.__setattr__(task_result, "status", TaskResultStatus.RUNNING)
            object.__setattr__(task_result, "started_at", started_at)
            object.__setattr__(task_result, "last_attempted_at", started_at)
            task_result.worker_ids.append(f"{self.worker_id}:{threading.get_ident()}")
        task_started.send(type(self), task_result=task_result)

        try:
            if task_result.task.takes_context:
                return_value = task_result.task.call(
                    TaskContext(task_result=task_result),
                    *task_result.args,
                    **task_result.kwargs,
                )
            else:
                return_value = task_result.task.call(*task_result.args, **task_result.kwargs)
            with self._lock:
                object.__setattr__(task_result, "_return_value", normalize_json(return_value))
                object.__setattr__(task_result, "status", TaskResultStatus.SUCCESSFUL)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            exception_type = type(exc)
            with self._lock:
                task_result.errors.append(
                    TaskError(
                        exception_class_path=f"{exception_type.__module__}.{exception_type.__qualname__}",
                        traceback="".join(format_exception(exc)),
                    )
                )
                object.__setattr__(task_result, "status", TaskResultStatus.FAILED)
        finally:
            with self._lock:
                object.__setattr__(task_result, "finished_at", timezone.now())
            task_finished.send(type(self), task_result=task_result)
            # Connections are thread-local. Close every configured alias, not
            # only the default connection, before returning the worker thread
            # to the pool.
            connections.close_all()

    def get_result(self, result_id):
        """Return a task result, whether pending, running, or finished."""
        with self._lock:
            task_result = self._results.get(result_id)
        if task_result is None:
            raise TaskResultDoesNotExist(result_id)
        return task_result

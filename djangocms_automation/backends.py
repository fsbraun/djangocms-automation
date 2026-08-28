"""A durable, database-backed task backend for Django's task framework.

Configure it as the default backend and run at least one worker process::

    TASKS = {
        "default": {
            "BACKEND": "djangocms_automation.backends.DatabaseBackend",
            "QUEUES": ["default"],
        }
    }

    $ python manage.py runworker

Unlike ``ImmediateBackend`` (inline, no durability) and
:class:`~djangocms_automation.utils.ThreadBackend` (in-process, lost on exit),
work enqueued here survives a restart and is picked up by whichever worker is
free. See :mod:`djangocms_automation.queue` for the trade-offs.
"""

from __future__ import annotations

from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TaskResult, TaskResultStatus
from django.utils.crypto import get_random_string
from django.utils.json import normalize_json

from .queue import QueuedTask

__all__ = ["DatabaseBackend"]


class DatabaseBackend(BaseTaskBackend):
    """Persist enqueued tasks to the database for a worker to execute."""

    supports_defer = True
    supports_priority = True
    #: Results are not readable across processes: the worker records success or
    #: failure, but the return value is not stored. Automations communicate
    #: through their own state models, not through task results.
    supports_get_result = False

    def __init__(self, alias, params):
        super().__init__(alias, params)
        self.worker_id = get_random_string(32)

    def enqueue(self, task, args, kwargs):
        self.validate_task(task)
        result_id = get_random_string(32)

        # Deferring the insert to commit would break the caller's expectation
        # that the row exists; instead the enqueue joins the caller's
        # transaction, so a rolled-back transaction takes the task with it.
        QueuedTask.objects.create(
            result_id=result_id,
            task_path=task.module_path,
            queue_name=task.queue_name,
            priority=task.priority,
            args=normalize_json(list(args)),
            kwargs=normalize_json(dict(kwargs)),
            run_after=task.run_after,
        )

        return TaskResult(
            task=task,
            id=result_id,
            status=TaskResultStatus.READY,
            enqueued_at=None,
            started_at=None,
            last_attempted_at=None,
            finished_at=None,
            args=args,
            kwargs=kwargs,
            backend=self.alias,
            errors=[],
            worker_ids=[],
        )

    async def aenqueue(self, task, args, kwargs):
        from asgiref.sync import sync_to_async

        return await sync_to_async(self.enqueue, thread_sensitive=True)(task, args, kwargs)

    def _run_now(self, task_row) -> None:
        """Execute one claimed row. Used by the worker command."""
        from django.utils.module_loading import import_string
        from django.utils.timezone import now

        try:
            task_obj = import_string(task_row.task_path)
            func = getattr(task_obj, "func", task_obj)
            # Deliberately *not* wrapped in a transaction. The engine commits the
            # claim, heartbeats, and state changes as it goes; holding one
            # transaction around the whole task would hide all of them until the
            # task returned, and its row locks would block cancellation and lease
            # recovery — so a hung action could never be timed out or recovered.
            func(*task_row.args, **task_row.kwargs)
        except BaseException as exc:  # noqa: BLE001 — recorded, never re-raised at the worker
            import traceback

            QueuedTask.objects.filter(pk=task_row.pk).update(
                state="FAILED",
                finished_at=now(),
                claimed_until=None,
                error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:20000],
            )
            raise
        QueuedTask.objects.filter(pk=task_row.pk).update(state="SUCCESSFUL", finished_at=now(), claimed_until=None)

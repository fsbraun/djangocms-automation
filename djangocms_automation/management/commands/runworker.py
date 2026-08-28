"""Worker process for :class:`djangocms_automation.backends.DatabaseBackend`.

Run one or more alongside the web process::

    python manage.py runworker

The worker claims tasks under a lease, executes them, and records the outcome.
It shuts down gracefully on ``SIGINT``/``SIGTERM``: the task in flight finishes
before the process exits, so a deploy does not strand work mid-execution.
"""

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils.crypto import get_random_string

from djangocms_automation.queue import QueuedTask

logger = logging.getLogger("djangocms_automation.worker")


class Command(BaseCommand):
    help = "Execute queued automation tasks from the database-backed queue."

    def add_arguments(self, parser):
        parser.add_argument("--queue", action="append", default=None, help="Queue to consume (repeatable).")
        parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to idle when the queue is empty.")
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain what is currently ready, then exit. Intended for tests and CI.",
        )
        parser.add_argument(
            "--max-tasks",
            type=int,
            default=0,
            help="Exit after this many tasks. 0 means run until stopped; use it to recycle workers.",
        )
        parser.add_argument(
            "--lease-seconds",
            type=int,
            default=300,
            help="How long a claimed task is held before another worker may take it.",
        )

    def handle(self, *args, **options):
        from djangocms_automation.backends import DatabaseBackend

        queues = options["queue"] or ["default"]
        worker_id = get_random_string(32)
        self._stopping = False

        def stop(signum, frame):
            if not self._stopping:
                self._stopping = True
                self.stdout.write("Shutdown requested; finishing the current task.")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, stop)
            except ValueError:  # pragma: no cover — not on the main thread
                pass

        backend = DatabaseBackend("default", {})
        processed = 0
        self.stdout.write(f"Worker {worker_id[:8]} consuming {', '.join(queues)}.")

        while not self._stopping:
            QueuedTask.release_expired()
            task_row = QueuedTask.claim_next(queues, worker_id, options["lease_seconds"])
            if task_row is None:
                if options["once"]:
                    break
                time.sleep(options["sleep"])
                close_old_connections()
                continue
            try:
                backend._run_now(task_row)
            except Exception:  # noqa: BLE001 — one bad task must not kill the worker
                logger.exception(
                    "automation.worker.task_failed",
                    extra={"queued_task_id": task_row.pk, "task_path": task_row.task_path},
                )
            processed += 1
            if options["max_tasks"] and processed >= options["max_tasks"]:
                break
            close_old_connections()

        self.stdout.write(self.style.SUCCESS(f"Worker stopped after {processed} task(s)."))

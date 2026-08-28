"""Durable task queue backing :class:`~djangocms_automation.backends.DatabaseBackend`.

Django 6 ships an immediate backend (runs inline) and a dummy one (runs never).
Neither survives a restart, so neither is a basis for production automations.
This module adds the missing piece: enqueued work is a database row, claimed by
a worker under a lease, and still there if the worker dies.

Using the application database rather than a broker is a deliberate trade. It
gives transactional enqueue for free — a task cannot become visible unless the
transaction that created it commits — and adds no infrastructure to deploy. It
is not built for very high throughput; a project at that volume should point
``TASKS`` at a broker-backed backend instead. Nothing else in this package
depends on which backend is configured.
"""

from __future__ import annotations

import datetime

from django.db import models
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

READY = "READY"
RUNNING = "RUNNING"
SUCCESSFUL = "SUCCESSFUL"
FAILED = "FAILED"

TASK_STATES = [
    (READY, _("Ready")),
    (RUNNING, _("Running")),
    (SUCCESSFUL, _("Successful")),
    (FAILED, _("Failed")),
]


class QueuedTask(models.Model):
    """One enqueued task awaiting, or having had, execution by a worker."""

    result_id = models.CharField(max_length=64, unique=True, verbose_name=_("Result id"))
    task_path = models.CharField(max_length=512, verbose_name=_("Task path"))
    queue_name = models.CharField(max_length=64, default="default", verbose_name=_("Queue"))
    priority = models.IntegerField(default=0, verbose_name=_("Priority"))
    args = models.JSONField(default=list, verbose_name=_("Positional arguments"))
    kwargs = models.JSONField(default=dict, verbose_name=_("Keyword arguments"))

    state = models.CharField(max_length=16, choices=TASK_STATES, default=READY, verbose_name=_("State"))
    run_after = models.DateTimeField(null=True, blank=True, verbose_name=_("Run after"))
    enqueued_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Enqueued at"))
    started_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Started at"))
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Finished at"))

    worker_id = models.CharField(max_length=64, blank=True, default="", verbose_name=_("Worker"))
    claimed_until = models.DateTimeField(null=True, blank=True, verbose_name=_("Claim expires"))
    attempts = models.PositiveIntegerField(default=0, verbose_name=_("Attempts"))
    error = models.TextField(blank=True, default="", verbose_name=_("Error"))

    class Meta:
        verbose_name = _("Queued task")
        verbose_name_plural = _("Queued tasks")
        ordering = ["-priority", "enqueued_at"]
        indexes = [
            models.Index(fields=["state", "queue_name", "run_after"]),
            models.Index(fields=["claimed_until"]),
        ]

    def __str__(self):
        return f"<QueuedTask {self.task_path} {self.state}>"

    @classmethod
    def claim_next(cls, queues, worker_id: str, lease_seconds: int = 300):
        """Claim the next runnable task for a worker, or return ``None``.

        Uses ``SELECT … FOR UPDATE SKIP LOCKED`` where the database supports it,
        so many workers can drain the same queue without contending on, or
        duplicating, a row. SQLite has no ``SKIP LOCKED``; its serialized writes
        make the same guarantee at the cost of concurrency, which is acceptable
        because SQLite is a development target here, not a production one.
        """
        from django.db import transaction

        timestamp = now()
        with transaction.atomic():
            queryset = (
                cls.objects.select_for_update(skip_locked=True)
                .filter(state=READY, queue_name__in=queues)
                .filter(models.Q(run_after__isnull=True) | models.Q(run_after__lte=timestamp))
                .order_by("-priority", "enqueued_at")
            )
            task_row = queryset.first()
            if task_row is None:
                return None
            task_row.state = RUNNING
            task_row.started_at = timestamp
            task_row.worker_id = worker_id
            task_row.attempts += 1
            task_row.claimed_until = timestamp + datetime.timedelta(seconds=lease_seconds)
            task_row.save(update_fields=["state", "started_at", "worker_id", "attempts", "claimed_until"])
            return task_row

    @classmethod
    def release_expired(cls, timestamp=None) -> int:
        """Return tasks whose worker died to the ready queue.

        Pairs with the engine's own lease recovery: this releases the *task*, the
        engine releases the *action*. Both are needed — a worker killed mid-task
        leaves a stranded row on each side.
        """
        timestamp = timestamp or now()
        return cls.objects.filter(state=RUNNING, claimed_until__lte=timestamp).update(
            state=READY, worker_id="", claimed_until=None, started_at=None
        )

    @classmethod
    def purge(cls, days: int = 7) -> int:
        """Delete finished task rows older than ``days``."""
        cutoff = now() - datetime.timedelta(days=days)
        deleted, _details = cls.objects.filter(state__in=(SUCCESSFUL, FAILED), finished_at__lte=cutoff).delete()
        return deleted

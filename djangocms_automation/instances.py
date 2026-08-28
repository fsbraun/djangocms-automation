import datetime
import hashlib

from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.timezone import now

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

User = get_user_model()

MAX_FIELD_LENGTH = 256


PENDING = "PENDING"
RUNNING = "RUNNING"
WAITING = "WAITING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELED = "CANCELED"

STATES = [
    (PENDING, _("Pending")),
    (RUNNING, _("Running")),
    (WAITING, _("Waiting")),
    (COMPLETED, _("Completed")),
    (FAILED, _("Failed")),
    (CANCELED, _("Canceled")),
]

#: States from which no further work is scheduled.
TERMINAL = frozenset({COMPLETED, FAILED, CANCELED})

#: States an execution can still leave under its own power.
ACTIVE = frozenset({PENDING, RUNNING, WAITING})


class AutomationInstance(models.Model):
    """Runtime instance of an automation execution.

    Tracks the state and data of a single automation run, including initial
    input data and accumulated results from executed actions.
    """

    automation_content = models.ForeignKey(
        "djangocms_automation.AutomationContent",
        blank=False,
        on_delete=models.CASCADE,
        verbose_name=_("Automation Content"),
    )
    status = models.CharField(
        max_length=20,
        choices=STATES,
        default=RUNNING,
        verbose_name=_("Status"),
    )
    initial_data = models.JSONField(
        verbose_name=_("Initial Data"),
        default=list,
    )
    data = models.JSONField(
        verbose_name=_("Data"),
        default=list,
    )
    key = models.CharField(
        verbose_name=_("Unique hash"),
        default="",
        max_length=64,
    )
    created = models.DateTimeField(
        auto_now_add=True,
    )
    updated = models.DateTimeField(
        auto_now=True,
    )
    finished = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Finished"),
    )
    idempotency_key = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name=_("Idempotency key"),
        help_text=_(
            "Caller-supplied key that prevents duplicate execution of the same "
            "event. An instance with a non-null key is unique per automation "
            "content."
        ),
    )

    def save(self, *args, **kwargs):
        self.key = self.get_key()
        return super().save(*args, **kwargs)

    def get_key(self) -> str:
        """Generate a unique SHA1 hash key for this instance.

        :returns: Hexadecimal SHA1 hash based on automation and instance IDs.
        :rtype: str
        """
        return hashlib.sha1(f"{self.automation_content.automation_id}-{self.id}".encode()).hexdigest()

    @classmethod
    def delete_history(cls, days: int = 30):
        """Delete finished automation instances older than specified days.

        :param days: Number of days to retain history. Defaults to 30.
        :type days: int
        :returns: Tuple of (number deleted, dict with deletion counts per model).
        :rtype: tuple
        """
        automations = cls.objects.filter(finished__isnull=False, updated__lt=now() - datetime.timedelta(days=days))
        return automations.delete()

    @classmethod
    def redact_payloads(cls, days: int) -> int:
        """Strip stored payloads from finished instances older than ``days``.

        A middle ground between keeping everything and deleting it: execution
        metadata (timings, states, attempt history) survives for auditing while
        the data that flowed through the run is dropped.

        :returns: The number of instances redacted.
        """
        cutoff = now() - datetime.timedelta(days=days)
        stale = cls.objects.filter(finished__isnull=False, updated__lt=cutoff).exclude(initial_data=[], data=[])
        stale_ids = list(stale.values_list("pk", flat=True))
        if not stale_ids:
            return 0
        AutomationAction.objects.filter(automation_instance_id__in=stale_ids).update(input_data=None, result={})
        return cls.objects.filter(pk__in=stale_ids).update(initial_data=[], data=[])

    def cancel(self, message: str = "Canceled") -> int:
        """Cancel this instance and every unfinished action within it.

        Idempotent: cancelling a finished instance is a no-op returning 0.

        :returns: The number of actions canceled.
        """
        from .transitions import transition_action

        if self.finished is not None:
            return 0
        canceled = 0
        open_actions = list(
            AutomationAction.objects.filter(automation_instance=self, finished__isnull=True).values_list(
                "pk", flat=True
            )
        )
        for action_id in open_actions:
            if transition_action(action_id, CANCELED, message=message[:MAX_FIELD_LENGTH], unfinished_only=True):
                canceled += 1
        updated = type(self).objects.filter(pk=self.pk, finished__isnull=True).update(status=CANCELED, finished=now())
        if updated:
            self.refresh_from_db()
            from .signals import instance_finished

            instance_finished.send(sender=type(self), instance=self, status=CANCELED)
        return canceled

    def __str__(self):
        return f"<AutomationInstance for {self.automation_content.automation.name} ({self.id})>"

    class Meta:
        verbose_name = _("Execution Instance")
        verbose_name_plural = _("Execution Instances")
        constraints = [
            models.UniqueConstraint(
                fields=["automation_content", "idempotency_key"],
                name="unique_idempotency_per_content",
                condition=models.Q(idempotency_key__isnull=False),
            ),
        ]


class AutomationAction(models.Model):
    """Individual action step within an automation execution.

    Represents a single plugin execution, tracking its state, timing,
    and any user interaction requirements.
    """

    automation_instance = models.ForeignKey(
        AutomationInstance,
        on_delete=models.CASCADE,
    )
    state = models.CharField(
        max_length=20,
        choices=STATES,
        default="PENDING",
        verbose_name=_("State"),
    )
    previous = models.ForeignKey(
        "djangocms_automation.AutomationAction",
        on_delete=models.SET_NULL,
        null=True,
        verbose_name=_("Previous action"),
    )
    parent = models.ForeignKey(
        "djangocms_automation.AutomationAction",
        on_delete=models.SET_NULL,
        null=True,
        related_name="children",
        verbose_name=_("Parent action"),
    )
    plugin_ptr = models.UUIDField(
        blank=True,
        verbose_name=_("Plugin UUID"),
    )
    paused_until = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Paused until"),
    )
    attempt_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Attempt count"),
    )
    max_attempts = models.PositiveIntegerField(
        default=1,
        verbose_name=_("Maximum attempts"),
    )
    next_attempt_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Next attempt at"),
    )
    started = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Started"),
    )
    heartbeat_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Last heartbeat"),
    )
    timeout_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Timeout in seconds"),
    )
    lease_id = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Execution lease"),
    )
    resumed = models.BooleanField(
        default=False,
        editable=False,
        verbose_name=_("Resumed"),
        help_text=_(
            "Marks the next claim as a continuation rather than a new attempt. Set when a waiting "
            "node is woken or a paused action is revived; cleared when the action is claimed."
        ),
    )
    re_entry_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Re-entry count"),
        help_text=_(
            "How often a waiting node resumed after its children finished. Counted separately from "
            "retry attempts: a split or agent that re-enters many times has not failed even once."
        ),
    )
    input_data = models.JSONField(
        null=True,
        blank=True,
        verbose_name=_("Input data"),
        help_text=_("The rows this action was given, recorded at claim time so it can be replayed."),
    )
    dead_lettered = models.BooleanField(
        default=False,
        verbose_name=_("Dead lettered"),
        help_text=_("Set when the action exhausted its attempts and awaits inspection or replay."),
    )
    dead_lettered_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Dead lettered at"),
    )
    replayed_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="replays",
        verbose_name=_("Replayed from"),
    )
    requires_interaction = models.BooleanField(default=False, verbose_name=_("Requires interaction"))
    interaction_user = models.ForeignKey(
        User,
        null=True,
        on_delete=models.PROTECT,
        verbose_name=_("Assigned user"),
    )
    interaction_group = models.ForeignKey(
        Group,
        null=True,
        on_delete=models.PROTECT,
        verbose_name=_("Assigned group"),
    )
    interaction_permissions = models.JSONField(
        default=list,
        verbose_name=_("Required permissions"),
        help_text=_("List of permissions of the form app_label.codename"),
    )
    created = models.DateTimeField(
        auto_now_add=True,
    )
    finished = models.DateTimeField(
        null=True,
    )
    message = models.CharField(
        max_length=MAX_FIELD_LENGTH,
        verbose_name=_("Message"),
        blank=True,
    )
    result = models.JSONField(
        verbose_name=_("Result"),
        null=True,
        blank=True,
        default=dict,
    )
    error_type = models.CharField(
        max_length=MAX_FIELD_LENGTH,
        blank=True,
        verbose_name=_("Error type"),
    )
    error_detail = models.TextField(
        blank=True,
        verbose_name=_("Error detail"),
    )

    @property
    def data(self):
        return self.automation_instance.data

    @property
    def duration(self):
        """Seconds between the start of the last attempt and finishing."""
        if self.started is None or self.finished is None:
            return None
        return (self.finished - self.started).total_seconds()

    def is_lease_expired(self, timestamp=None) -> bool:
        """Check whether a ``RUNNING`` action's worker appears to be gone.

        True when the action has been running past its own timeout, or has not
        refreshed its heartbeat within the configured lease window.
        """
        from django.conf import settings

        if self.state != RUNNING:
            return False
        timestamp = timestamp or now()
        if self.timeout_seconds and self.started:
            if (timestamp - self.started).total_seconds() > self.timeout_seconds:
                return True
        window = getattr(settings, "AUTOMATION_LEASE_SECONDS", 300)
        reference = self.heartbeat_at or self.started
        if reference is None:
            return False
        return (timestamp - reference).total_seconds() > window

    def hours_since_created(self) -> float:
        """Calculate hours elapsed since action creation.

        :returns: Hours since creation, or 0 if action is finished.
        :rtype: float
        """
        if self.finished:
            return 0
        return (now() - self.created).total_seconds() / 3600

    def get_previous_tasks(self) -> list["AutomationAction"]:
        """Retrieve the previous action(s) in the execution chain.

        :returns: List of previous AutomationAction instances. For join
            points (actions that fanned out into branches), returns the
            branch actions; otherwise returns the single previous action.
        :rtype: list[AutomationAction]
        """
        children = list(self.children.all())
        if children:
            return children
        return [self.previous] if self.previous else []

    @classmethod
    def get_open_tasks(cls, user) -> tuple["AutomationAction", ...]:
        """Get all open tasks requiring interaction that the user can access.

        :param user: The user to check permissions for.
        :type user: User
        :returns: Tuple of AutomationAction instances awaiting user interaction.
        :rtype: tuple[AutomationAction, ...]
        """
        candidates = cls.objects.filter(finished=None, requires_interaction=True)
        return tuple(task for task in candidates if user in task.get_users_with_permission())

    def get_users_with_permission(
        self,
        include_superusers: bool = True,
        backend: str = "django.contrib.auth.backends.ModelBackend",
    ):
        """Get users who have permission to interact with this action.

        Filters users based on required permissions, assigned user/group,
        and optionally includes superusers.

        :param include_superusers: Include superusers regardless of permissions.
            Defaults to True.
        :type include_superusers: bool
        :param backend: Authentication backend to use for permission checking.
        :type backend: str
        :returns: QuerySet of User instances with applicable permissions.
        :rtype: QuerySet[User]
        """
        users = User.objects.all()
        for permission in self.interaction_permissions:
            users &= User.objects.with_perm(permission, include_superusers=False, backend=backend)
        if self.interaction_user is not None:
            users = users.filter(id=self.interaction_user_id)
        if self.interaction_group is not None:
            users = users.filter(groups=self.interaction_group)
        if include_superusers:
            users |= User.objects.filter(is_superuser=True)
        return users

    def __str__(self):
        return f"<ATM {self.plugin_ptr} {self.message} ({self.id})>"

    def __repr__(self):
        return self.__str__()


class AutomationActionEvent(models.Model):
    """Immutable audit event for an action state transition."""

    action = models.ForeignKey(
        AutomationAction,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_("Action"),
    )
    from_state = models.CharField(
        max_length=20,
        choices=STATES,
        verbose_name=_("Previous state"),
    )
    to_state = models.CharField(
        max_length=20,
        choices=STATES,
        verbose_name=_("New state"),
    )
    attempt = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Attempt"),
    )
    lease_id = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Execution lease"),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Metadata"),
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created", "pk")
        verbose_name = _("Action event")
        verbose_name_plural = _("Action events")

    def __str__(self):
        return f"{self.action_id}: {self.from_state} → {self.to_state}"


class DeadLetter(AutomationAction):
    """Dead-lettered actions: those that exhausted their attempts.

    A proxy rather than a separate table — the failed action already carries the
    full record (input, attempts, error, event history), so copying it into a
    second model would only create two versions of the same truth. The proxy
    exists to give the queue its own admin entry and permissions.
    """

    class Meta:
        proxy = True
        verbose_name = _("Dead letter")
        verbose_name_plural = _("Dead letters")


class SchedulerLock(models.Model):
    """A short-lived, database-backed mutex for the scheduler.

    ``runautomations`` may be installed on several hosts for availability. Only
    one of them should revive actions, recover leases, or fire timers in a given
    tick, or a timer fires twice and a recovered action is enqueued twice.

    The lock is a single row per name held until ``locked_until``. Acquisition is
    one conditional ``UPDATE``, so it is atomic on every supported database
    without advisory locks or an external coordinator. A holder that dies simply
    lets the lease expire.
    """

    name = models.CharField(max_length=64, unique=True, verbose_name=_("Lock name"))
    holder = models.UUIDField(null=True, blank=True, verbose_name=_("Holder"))
    locked_until = models.DateTimeField(null=True, blank=True, verbose_name=_("Locked until"))

    class Meta:
        verbose_name = _("Scheduler lock")
        verbose_name_plural = _("Scheduler locks")

    def __str__(self):
        return f"<SchedulerLock {self.name} until {self.locked_until}>"

    @classmethod
    def acquire(cls, name: str, ttl_seconds: int = 300):
        """Try to take the named lock.

        :returns: The holder token if the lock was taken, otherwise ``None``.
        """
        import uuid as uuid_module

        timestamp = now()
        expiry = timestamp + datetime.timedelta(seconds=ttl_seconds)
        token = uuid_module.uuid4()
        cls.objects.get_or_create(name=name)
        taken = cls.objects.filter(
            models.Q(locked_until__isnull=True) | models.Q(locked_until__lte=timestamp),
            name=name,
        ).update(holder=token, locked_until=expiry)
        return token if taken else None

    @classmethod
    def release(cls, name: str, token) -> bool:
        """Release the named lock if ``token`` still holds it."""
        return bool(cls.objects.filter(name=name, holder=token).update(holder=None, locked_until=None))

import datetime
import hashlib

from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.timezone import now

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

User = get_user_model()

MAX_FIELD_LENGTH = 256


class AutomationInstance(models.Model):
    automation = models.ForeignKey(
        "djangocms_automation.Automation",
        blank=False,
        on_delete=models.CASCADE,
        verbose_name=_("Automation"),
    )
    testing = models.ForeignKey(
        "djangocms_automation.AutomationContent",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        verbose_name=_("Testing Content"),
    )
    data = models.JSONField(
        verbose_name=_("Data"),
        default=dict,
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

    def save(self, *args, **kwargs):
        self.key = self.get_key()
        return super().save(*args, **kwargs)

    def get_key(self):
        return hashlib.sha1(f"{self.automation_id}-{self.id}".encode("utf-8")).hexdigest()

    @classmethod
    def delete_history(cls, days=30):
        automations = cls.objects.filter(finished=True, updated__lt=now() - datetime.timedelta(days=days))
        return automations.delete()

    def __str__(self):
        return f"<AutomationInstance for {self.automation}>"


class AutomationAction(models.Model):
    automation_instance = models.ForeignKey(
        AutomationInstance,
        on_delete=models.CASCADE,
    )
    previous = models.ForeignKey(
        "djangocms_automation.AutomationAction",
        on_delete=models.SET_NULL,
        null=True,
        verbose_name=_("Previous action"),
    )
    status = models.UUIDField(
        blank=True,
        verbose_name=_("Status"),
    )
    paused_until = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Paused until"),
    )
    locked = models.IntegerField(
        default=0,
        verbose_name=_("Locked"),
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

    @property
    def data(self):
        return self.automation.data

    def hours_since_created(self) -> float:
        """returns the number of hours since creation of node, 0 if finished"""
        if self.finished:
            return 0
        return (now() - self.created).total_seconds() / 3600

    def get_previous_tasks(self) -> list["AutomationAction"]:
        if self.message == "Joined" and self.result:
            return self.__class__.objects.filter(id__in=self.result)
        return [self.previous] if self.previous else []

    @classmethod
    def get_open_tasks(cls, user):
        candidates = cls.objects.filter(finished=None, requires_interaction=True)
        return tuple(task for task in candidates if user in task.get_users_with_permission())

    def get_users_with_permission(
        self,
        include_superusers=True,
        backend="django.contrib.auth.backends.ModelBackend",
    ):
        """
        Given an AutomationTaskModel instance, which has access to a list of permission
        codenames (self.interaction_permissions), the assigned user (self.interaction_user),
        and assigned group (self.interaction_group), returns a QuerySet of users with
        applicable permissions that meet the requirements for access.
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
        return f"<ATM {self.status} {self.message} ({self.id})>"

    def __repr__(self):
        return self.__str__()

from collections import defaultdict
import uuid

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from cms.models import CMSPlugin
from cms.models.fields import PlaceholderRelationField

from .instances import AutomationAction  # noqa F401
from .services import service_registry
from .triggers import trigger_registry
from .widgets import ConditionBuilderWidget


class AutomationContent(models.Model):
    automation = models.ForeignKey("djangocms_automation.Automation", related_name="contents", on_delete=models.CASCADE)
    description = models.TextField()

    placeholders = PlaceholderRelationField()

    def get_title(self):
        return self.automation.name

    def get_description(self):
        return self.description

    def __str__(self):
        return self.get_title()

    def get_template(self):
        return None

    def get_placeholder_slots(self):
        return list(self.triggers.values_list("slot", flat=True))


class Automation(models.Model):
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class AutomationTrigger(models.Model):
    automation_content = models.ForeignKey(AutomationContent, related_name="triggers", on_delete=models.CASCADE)
    slot = models.SlugField(
        verbose_name=_("Slot"),
        help_text=_("Unique identifier for this trigger within the automation content. Used, e.g., if it needs to be triggered by other automation."),
        max_length=255,
    )
    type = models.CharField(max_length=100, default="code")
    config = models.JSONField(default=dict, editable=False)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position"]

    def get_definition(self):
        return trigger_registry.get(self.type)

    def __str__(self):
        type = trigger_registry.get(self.type)
        return f"{self.slot.capitalize()} ({type.name if type else 'unknown'})"


class APIKey(models.Model):
    """Store named API keys for external services."""

    name = models.CharField(
        max_length=255,
        verbose_name=_("Name"),
        help_text=_("Descriptive name for this API key"),
    )
    service = models.CharField(
        max_length=100,
        verbose_name=_("Service"),
        help_text=_("The service this API key is for"),
    )
    api_key = models.CharField(
        max_length=500,
        verbose_name=_("API Key"),
        help_text=_("The API key or token"),
    )
    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
        help_text=_("Optional notes about this API key"),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
        help_text=_("Whether this API key is active"),
    )
    created = models.DateTimeField(auto_now_add=True, verbose_name=_("Created"))
    updated = models.DateTimeField(auto_now=True, verbose_name=_("Updated"))

    class Meta:
        verbose_name = _("API Key")
        verbose_name_plural = _("API Keys")
        ordering = ["service", "name"]
        indexes = [
            models.Index(fields=["service", "is_active"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_service_display()})"

    def get_service_display(self):
        """Get the human-readable service name."""
        service = service_registry.get(self.service)
        return service["name"] if service else self.service

    @classmethod
    def get_service_choices(cls):
        """Get available service choices."""
        return service_registry.get_choices()


class AutomationPluginModel(CMSPlugin):
    """Base model for automation plugins."""

    class Meta:
        abstract = True

    uuid = models.UUIDField(
        unique=True,
        editable=False,
        verbose_name=_("UUID"),
        default=uuid.uuid4,
    )
    comment = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Comment"),
        help_text=_("Optional comment about this automation step"),
    )

    def execute(self, action: AutomationAction, single_step: bool = False):
        """Execute the plugin logic for the given action."""
        raise NotImplementedError("Subclasses must implement the execute method.")


class IfPluginModel(AutomationPluginModel):
    """Model for 'If' automation plugin."""

    condition = models.JSONField(
        verbose_name=_("Condition"),
        help_text=_("Condition to evaluate for this conditional to evaluate. Use double curly braces {{ }} for data attribute resolution, e.g. {{ first_name }}."),
        default=dict,
    )


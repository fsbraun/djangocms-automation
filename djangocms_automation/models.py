from functools import cache
from django.db import models
from django.utils.translation import gettext_lazy as _

from cms.models.fields import PlaceholderRelationField

from .instances import AutomationInstance, AutomationAction  # noqa F401
from .services import service_registry


class Automation(models.Model):
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class AutomationContent(models.Model):
    automation = models.ForeignKey(Automation, related_name="contents", on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    body = models.TextField()

    placeholders = PlaceholderRelationField()

    def get_title(self, lang):
        return self.title

    def __str__(self):
        return self.title

    def get_placeholder_slots(self):
        return list(self.placeholders.values_list("slot", flat=True))


class AutomationTrigger(models.Model):
    automation_content = models.ForeignKey(AutomationContent, related_name="triggers", on_delete=models.CASCADE)
    slug = models.SlugField(
        verbose_name=_("Slug"),
        help_text=_("Unique identifier for this trigger within the automation content. Used, e.g., if it needs to be triggered by other automation."),
        max_length=255,
    )
    type = models.CharField(max_length=100)
    # config = models.JSONField(default=dict, editable=False)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position"]

    def get_definition(self):
        from .triggers import trigger_registry

        return trigger_registry.get(self.type)

    def __str__(self):
        return f"{self.automation_content.automation.name} - {self.slug}"


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

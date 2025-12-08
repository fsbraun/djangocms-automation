import uuid

from django.db import models, transaction
from django.utils.translation import gettext_lazy as _

from cms.models import CMSPlugin, Placeholder
from cms.models.fields import PlaceholderRelationField

from .instances import AutomationInstance, AutomationAction, RUNNING, PENDING, COMPLETED  # noqa F401
from .services import service_registry
from .triggers import trigger_registry


class AutomationContent(models.Model):
    """Container for all versioned content of an automation.

    Holds placeholders and triggers for potentially different versions of an automation.
    Exists only in the site's default language (not translatable).
    """

    automation = models.ForeignKey(
        "djangocms_automation.Automation", related_name="contents", on_delete=models.CASCADE
    )
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
    """Top-level automation workflow definition."""

    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("Automation")
        verbose_name_plural = _("Automations")


class AutomationTrigger(models.Model):
    """Entry point that initiates an automation workflow execution."""

    automation_content = models.ForeignKey(AutomationContent, related_name="triggers", on_delete=models.CASCADE)
    slot = models.SlugField(
        verbose_name=_("Slot"),
        help_text=_(
            "Unique identifier for this trigger within the automation content. Used, e.g., if it needs to be triggered by other automation."
        ),
        max_length=255,
    )
    type = models.CharField(max_length=100, default="code")
    config = models.JSONField(default=dict, editable=False)
    position = models.PositiveIntegerField(default=0, verbose_name="#")

    class Meta:
        verbose_name = _("Trigger")
        verbose_name_plural = _("Triggers")
        ordering = ["position"]

    def get_definition(self):
        return trigger_registry.get(self.type)

    def trigger_execution(self, data=None, start: bool = True):
        placeholder = Placeholder.objects.get_for_obj(self.automation_content).get(slot=self.slot)
        plugin = placeholder.get_plugins().first()
        plugin, _ = plugin.get_plugin_instance()

        with transaction.atomic():
            instance = AutomationInstance.objects.create(
                automation_content=self.automation_content,
                data=data or [],
                initial_data=data or [],
            )
            action = AutomationAction.objects.create(
                previous=None,
                parent=None,
                automation_instance=instance,
                plugin_ptr=plugin.uuid,
                finished=None,
            )
        if start:
            from .tasks import execute_action

            execute_action.enqueue(action.pk, data=data)

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
        verbose_name = _("Secret")
        verbose_name_plural = _("Secrets")
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

    def execute(self, action: AutomationAction, data: dict, single_step: bool = False, **kwargs):
        """Execute the plugin logic for the given action."""
        raise NotImplementedError("Subclasses must implement the execute method.")

    def get_next_actions(self, action: AutomationAction):
        """Get the next action based on the plugin structure."""
        if action.state != COMPLETED:
            return []

        next_plugin = self.next_plugin_instance
        if next_plugin:
            # Only create action if the plugin has uuid (is an AutomationPluginModel)
            if hasattr(next_plugin, "uuid"):
                return [
                    AutomationAction.objects.create(
                        previous=action,
                        parent=action.parent,
                        automation_instance=action.automation_instance,
                        plugin_ptr=next_plugin.uuid,
                        finished=None,
                    )
                ]
        return []


class ConditionalPluginModel(AutomationPluginModel):
    """Plugin model for conditional branching based on evaluated expressions."""

    question = models.CharField(
        max_length=255,
        verbose_name=_("Question"),
        blank=True,
        help_text=_(
            "The question this conditional answers, e.g., 'Is the user active?' It will be shown in the editor."
        ),
    )
    condition = models.JSONField(
        verbose_name=_("Condition"),
        help_text=_(
            "Condition to evaluate for this conditional to evaluate. Use double curly braces {{ }} for data attribute resolution, e.g. {{ first_name }}."
        ),
        default=dict,
    )

    no_yes_channel = _(
        'No "Yes" channel defined. The yes channel determines which actions will be executed if the condition is met. '
        'Please add a "Yes" branch to this conditional in the structure board.'
    )
    no_no_channel = _(
        'No "No" channel defined. The no channel determines which actions will be executed if the condition is not met. '
        'Please add a "No" branch to this conditional in the structure board.'
    )
    multiple_channels = _(
        'Both the "Yes" and "No" cannot be defined more than once. Please make sure only one branch is present '
        "for both of them for this conditional."
    )

    def messages(self):
        messages = []
        yes_channels = [child for child in self.child_plugin_instances if child.plugin_type == "ThenPlugin"]
        no_channels = [child for child in self.child_plugin_instances if child.plugin_type == "ElsePlugin"]
        if len(yes_channels) == 0:
            messages.append(self.no_yes_channel)
        if len(no_channels) == 0:
            messages.append(self.no_no_channel)
        if len(yes_channels) > 1 or len(no_channels) > 1:
            messages.append(self.multiple_channels)
        return messages


class SplitPluginModel(AutomationPluginModel):
    """Plugin model for parallel execution of multiple workflow paths."""

    class Meta:
        verbose_name = _("Split Plugin")
        verbose_name_plural = _("Split Plugins")

    no_paths = _(
        "No paths have been added to this split. Each split needs at least one path to continue the automation flow. "
        "Please add at least one path plugin to this split plugin in the structure board."
    )

    def messages(self):
        if not self.child_plugin_instances or len(self.child_plugin_instances) == 0:
            return [self.no_paths]
        return []

    def get_next_actions(self, action: AutomationAction):
        if action.state == RUNNING and not action.children.exists():
            next_actions = []
            for child in self.child_plugin_instances:
                if child.plugin_type == "AutomationPath" and child.child_plugin_instances:
                    # Downcast to get the actual plugin instance with uuid
                    child_instance, _ = child.child_plugin_instances[0].get_plugin_instance()
                    next_actions.append(
                        AutomationAction.objects.create(
                            previous=action,
                            parent=action,
                            automation_instance=action.automation_instance,
                            plugin_ptr=child_instance.uuid,
                            finished=None,
                        )
                    )
            return next_actions
        return super().get_next_actions(action)

    def execute(
        self,
        action: AutomationAction,
        data: dict,
        single_step: bool = False,
        plugin_dict: dict[CMSPlugin] | None = None,
    ):
        path_ends = [
            child.child_plugin_instances[-1] for child in self.child_plugin_instances if child.child_plugin_instances
        ]
        path_ends = AutomationAction.objects.filter(
            plugin_ptr__in=[p.uuid for p in path_ends], automation_instance=action.automation_instance
        )
        completed = all(path.finished for path in path_ends)
        if path_ends and completed:
            return COMPLETED, {}
        return RUNNING, {}


class BaseActionPluginModel(AutomationPluginModel):
    """Base model for action plugins that perform tasks."""

    pass

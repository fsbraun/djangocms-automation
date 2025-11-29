"""Custom form widgets and fields for djangocms_automation.

Provides a Select widget for choosing a registered Trigger with:
- Option title tooltips containing the trigger description.
- A dynamic <details> block showing the JSON schema of the currently
  selected trigger.

The widget renders a small inline script (no external dependency) to
swap the schema display when the selection changes.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import AutomationTrigger
from .triggers import trigger_registry
from . import widgets


class TriggerChoiceField(forms.ChoiceField):
    """Choice field bound to trigger registry using TriggerSelectWidget."""

    widget = widgets.TriggerSelectWidget

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("choices", trigger_registry.get_choices())
        super().__init__(*args, **kwargs)

    def valid_value(self, value):  # Strict registry membership
        return trigger_registry.get(value) is not None


class AutomationTriggerAdminForm(forms.ModelForm):
    """Custom form for AutomationTrigger with trigger type selector and dynamic config fields.

    This form dynamically adds trigger-specific configuration fields based on the
    selected trigger type. The configuration values are stored in the config JSON field.
    """

    type = TriggerChoiceField(
        label=_("Trigger type"),
    )

    class Meta:
        model = AutomationTrigger
        fields = ("automation_content", "type", "slot", "position")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Custom queryset: nur Inhalte aktiver Automationen
        from .models import AutomationContent

        self.fields["automation_content"].queryset = AutomationContent.admin_manager.select_related(
            "automation"
        ).current_content()
        self.fields["automation_content"].widget = forms.HiddenInput()
        self.fields["position"].widget = forms.HiddenInput()

    def clean(self):
        """Validate and prepare config data."""
        cleaned_data = super().clean()

        # Extract config fields
        config = {}
        trigger_type = cleaned_data.get("type")

        if trigger_type:
            trigger_class = trigger_registry.get(trigger_type)
            if trigger_class:
                for field_name in trigger_class.declared_fields.keys():
                    if field_name in cleaned_data:
                        value = cleaned_data[field_name]
                        # Convert datetime objects to ISO strings for JSON storage
                        if hasattr(value, "isoformat"):
                            value = value.isoformat()
                        config[field_name] = value

        # Store config in cleaned_data so it can be saved
        cleaned_data["_config"] = config
        return cleaned_data

    def save(self, commit=True):
        """Save the instance with config data."""
        instance = super().save(commit=False)

        # Set config from cleaned data
        if hasattr(self, "cleaned_data") and "_config" in self.cleaned_data:
            instance.config = self.cleaned_data["_config"]

        if commit:
            instance.save()

        return instance


class ConditionalPluginForm(forms.ModelForm):
    """Custom form for ConditionalPlugin with ConditionBuilderWidget for condition field."""

    class Meta:
        model = AutomationTrigger
        fields = "__all__"
        widgets = {
            "condition": widgets.ConditionBuilderWidget,
        }

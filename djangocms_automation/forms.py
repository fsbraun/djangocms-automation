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
        kwargs.setdefault('choices', trigger_registry.get_choices())
        super().__init__(*args, **kwargs)

    def valid_value(self, value):  # Strict registry membership
        return _get_trigger(value) is not None


class AutomationTriggerAdminForm(forms.ModelForm):
    """Custom form for AutomationTrigger with trigger type selector and filtered automation content."""

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
        self.fields["automation_content"].queryset = (
            AutomationContent.admin_manager.select_related("automation").current_content()
        )
        self.fields["automation_content"].widget = forms.HiddenInput()  # Hidden, set via URL or context


class ConditionalPluginForm(forms.ModelForm):
    """Custom form for ConditionalPlugin with ConditionBuilderWidget for condition field."""

    class Meta:
        model = AutomationTrigger
        fields = "__all__"
        widgets = {
            "condition": widgets.ConditionBuilderWidget,
        }


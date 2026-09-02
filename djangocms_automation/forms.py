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
from django.utils.dateparse import parse_date, parse_datetime, parse_time
from django.utils.translation import gettext_lazy as _

from . import widgets
from .models import AutomationTrigger, ConditionalPluginModel, LoopPluginModel
from .triggers import trigger_registry


class TriggerChoiceField(forms.ChoiceField):
    """Choice field bound to trigger registry using TriggerSelectWidget."""

    widget = widgets.TriggerSelectWidget

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("choices", trigger_registry.get_choices())
        super().__init__(*args, **kwargs)

    def valid_value(self, value):  # Strict registry membership
        return trigger_registry.get(value) is not None


def _from_config(field, value):
    """A stored config value, in the shape its form field expects back.

    ``clean`` writes datetimes out with ``isoformat`` because JSON has no such
    type. Reading them back as strings is not merely untidy: a widget that
    splits a value across two inputs calls ``decompress`` on it, and a string
    has no ``utcoffset``, so opening a timer trigger raised ``AttributeError``
    before the page could render.

    Anything unparseable is passed through as it stands — a form field
    reporting a bad value is more use than a config key silently emptied.
    """
    if not isinstance(value, str):
        return value
    if isinstance(field, (forms.SplitDateTimeField, forms.DateTimeField)):
        return parse_datetime(value) or parse_date(value) or value
    if isinstance(field, forms.DateField):
        return parse_date(value) or value
    if isinstance(field, forms.TimeField):
        return parse_time(value) or value
    return value


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

        # Config fields are declared on the trigger definition, not on the
        # model, so nothing seeds them from the stored value. Without this the
        # form does not merely look empty: ``clean`` rebuilds the config from
        # what was submitted, so opening a trigger and saving it unchanged
        # erases its settings.
        config = getattr(self.instance, "config", None) or {}
        for name in config:
            if name in self.fields and name not in self.initial:
                self.initial[name] = _from_config(self.fields[name], config[name])

    def clean(self):
        """Validate and prepare config data."""
        cleaned_data = super().clean()

        # Extract config fields
        config = {}
        trigger_type = cleaned_data.get("type")

        if trigger_type:
            trigger_class = trigger_registry.get(trigger_type)
            if trigger_class:
                for field_name in trigger_class.declared_fields:
                    if field_name in cleaned_data:
                        value = cleaned_data[field_name]
                        # Convert datetime objects to ISO strings for JSON storage
                        if hasattr(value, "isoformat"):
                            value = value.isoformat()
                        config[field_name] = value

        # Webhook triggers always need a token; generate one if left empty.
        if trigger_type:
            from .triggers import WebhookTrigger, generate_webhook_token

            trigger_class = trigger_registry.get(trigger_type)
            if trigger_class and issubclass(trigger_class, WebhookTrigger) and not config.get("token"):
                config["token"] = generate_webhook_token()

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
        model = ConditionalPluginModel
        fields = "__all__"
        widgets = {
            "condition": widgets.ConditionBuilderWidget,
        }


class LoopPluginForm(forms.ModelForm):
    """Custom form for LoopPlugin with ConditionBuilderWidget for condition field."""

    class Meta:
        model = LoopPluginModel
        fields = "__all__"
        widgets = {
            "condition": widgets.ConditionBuilderWidget,
        }


class MailActionDataForm(forms.Form):
    """Data form for MailAction plugin with email-specific fields.

    ``subject``, ``recipient_email`` and ``from_email`` are expressions
    (literal or data path); ``body`` is a template (Textarea widget) with
    ``{{ dotted.path }}`` substitution.
    """

    subject = forms.CharField(label=_("Email Subject"), max_length=255, required=True)
    body = forms.CharField(
        label=_("Email Body"),
        widget=forms.Textarea,
        required=True,
    )
    body_format = forms.ChoiceField(
        label=_("Body format"),
        required=False,
        choices=(("", _("Plain text")), ("html", _("HTML"))),
        help_text=_(
            "HTML sends both: the markup for clients that render it, and a plain-text version for those that do not."
        ),
    )
    recipient_email = forms.EmailField(label=_("Recipient Email"), required=True)
    from_email = forms.EmailField(
        label=_("Sender Email"),
        required=False,
        help_text=_("Optional. Defaults to the site's DEFAULT_FROM_EMAIL."),
    )


class RunNowForm(forms.Form):
    """Start a run by hand, from the toolbar.

    An automation that cannot be started without a webhook, a cron tick or a
    Django shell is one nobody tries before shipping it. This is the button
    that makes trying it the easy thing to do.
    """

    trigger = forms.ModelChoiceField(
        queryset=AutomationTrigger.objects.none(),
        label=_("Trigger"),
        help_text=_("Which entry point to start from. Its data schema decides what may be sent."),
    )
    data = forms.JSONField(
        label=_("Starting data"),
        required=False,
        initial=list,
        widget=forms.Textarea(attrs={"rows": 8, "class": "vLargeTextField"}),
        help_text=_(
            "The rows the run starts with, as a JSON array of objects. "
            "A single object is taken as one row. Leave empty to start with none."
        ),
    )

    def __init__(self, *args, automation_content=None, **kwargs):
        super().__init__(*args, **kwargs)
        triggers = AutomationTrigger.objects.filter(automation_content=automation_content).order_by("position")
        self.fields["trigger"].queryset = triggers
        if len(triggers) == 1:
            # Nothing to choose. Shown anyway, so the run says what it started.
            self.fields["trigger"].initial = triggers[0]

    def clean(self):
        cleaned = super().clean()
        trigger = cleaned.get("trigger")
        if trigger is None:
            return cleaned

        from .engine import normalize_rows

        rows = normalize_rows(cleaned.get("data"))
        if any(not isinstance(row, dict) for row in rows):
            raise forms.ValidationError({"data": _("Every row must be a JSON object.")})

        # The same check an inbound webhook gets. A manual run that skipped it
        # would pass data through that the real entry point would refuse, and
        # so would prove the automation works when it does not.
        definition = trigger.get_definition()
        if definition is not None:
            handler = definition()  # the registry holds classes; the webhook view does the same
            for position, row in enumerate(rows, start=1):
                try:
                    handler.validate_payload(row, config=trigger.config)
                except Exception as exc:  # jsonschema's ValidationError, or ValueError without it
                    raise forms.ValidationError(
                        {
                            "data": _("Row %(number)d does not match the trigger's schema: %(error)s")
                            % {"number": position, "error": exc}
                        }
                    ) from exc
        cleaned["rows"] = rows
        return cleaned

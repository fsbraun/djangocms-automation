"""Custom form widgets and fields for djangocms_automation.

Provides a Select widget for choosing a registered Trigger with:
- Option title tooltips containing the trigger description.
- A dynamic <details> block showing the JSON schema of the currently
  selected trigger.

The widget renders a small inline script (no external dependency) to
swap the schema display when the selection changes.
"""

from __future__ import annotations

import json
from typing import Iterable

from django import forms
from django.utils.safestring import mark_safe
from django.utils.html import escape
from django.utils.translation import gettext_lazy as _

from .triggers import trigger_registry, Trigger


def _get_trigger(trigger_id: str) -> Trigger | None:
    return trigger_registry.get(trigger_id)


class TriggerSelectWidget(forms.Select):
	"""Select widget for triggers that shows description + schema.

	Enhancements over a plain Select:
	- Prominent description display below the select.
	- Collapsible <details> block for JSON schema inspection.
	- Uses external JS file to update description and schema without reload.
	"""

	template_name = ""  # not using Django's template-based rendering here

	class Media:
		js = ('djangocms_automation/js/trigger_select.js',)

	def __init__(self, attrs=None, choices: Iterable = ()):  # choices ignored, taken from registry
		super().__init__(attrs, choices=self._build_choices())

	def _build_choices(self):
		return trigger_registry.get_choices()

	def render(self, name, value, attrs=None, renderer=None):  # noqa: D401 - short override
		if value is None:
			value = ""
		attrs = self.build_attrs(self.attrs, attrs)

		# Add data attribute with registry for JS
		registry_json = self._js_registry_json()
		attrs['data-trigger-registry'] = registry_json

		select_html_parts = [f'<select name="{escape(name)}"']
		for k, v in attrs.items():
			select_html_parts.append(f' {escape(k)}="{escape(v)}"')
		select_html_parts.append(' id="trigger-select"')
		select_html_parts.append('>')

		# Build option tags (no title tooltips - description shown below)
		for trigger_id, label in self._build_choices():
			selected = ' selected' if str(value) == str(trigger_id) else ''
			select_html_parts.append(
				f'<option value="{escape(trigger_id)}"{selected}>{escape(label)}</option>'
			)
		select_html_parts.append('</select>')

		# Current trigger description and schema
		current_trigger = _get_trigger(str(value)) or (
			_get_trigger(self._build_choices()[0][0]) if self._build_choices() else None
		)
		if current_trigger:
			schema_json = json.dumps(current_trigger.data_schema, indent=2, ensure_ascii=False)
			desc_html = escape(current_trigger.description)
		else:
			schema_json = '{}'
			desc_html = _('No trigger selected.')

		info_block = (
			f'<div id="trigger-description" style="margin-top:0.75rem; padding:0.5rem; background:#f0f7ff; border-left:3px solid #0066cc; font-size:14px;">{desc_html}</div>'
			'<details id="trigger-schema-details" style="margin-top:0.5rem;">'
			f'<summary style="cursor:pointer; font-weight:500;">{_("Schema details for input data")}</summary>'
			f'<pre id="trigger-schema" style="margin-top:0.5rem; background:#f8f9fa; border:1px solid #ddd; padding:0.5rem; max-height:24rem; overflow:auto; font-size:12px;">{escape(schema_json)}</pre>'
			'</details>'
		)

		html = ''.join(select_html_parts) + info_block
		return mark_safe(html)

	def _js_registry_json(self) -> str:
		mapping = {}
		for trigger_id, _label in self._build_choices():
			trig = _get_trigger(trigger_id)
			if trig:
				schema_str = json.dumps(trig.data_schema, indent=2, ensure_ascii=False)
				mapping[trigger_id] = {
					"description": trig.description,
					"schema": schema_str,
				}
		return json.dumps(mapping, ensure_ascii=False)


class TriggerChoiceField(forms.ChoiceField):
    """Choice field bound to trigger registry using TriggerSelectWidget."""

    widget = TriggerSelectWidget

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
        from .models import AutomationTrigger
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


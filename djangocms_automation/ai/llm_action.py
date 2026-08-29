"""LLM action: run a prompt against a configured LLM (provider-independent)."""

from __future__ import annotations

import datetime
import json

from django import forms
from django.conf import settings
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from .. import llm
from ..engine import ActionPause
from ..models import BaseActionPluginModel
from ..utilities.templates import safe_render, validate_template

#: Give up after this many rate-limit pauses.
MAX_LLM_RETRIES = 5


def _model_choices():
    return [(model, model) for model in llm.get_allowed_llm_models()]


def _validate_json_schema(value):
    if not value:
        return
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise forms.ValidationError(_("Invalid JSON: %(error)s") % {"error": exc})
    if not isinstance(parsed, dict):
        raise forms.ValidationError(_("The schema must be a JSON object."))
    if parsed.get("type") == "object" and parsed.get("additionalProperties") is not False:
        raise forms.ValidationError(_('Object schemas must set "additionalProperties": false.'))


class LLMActionForm(forms.Form):
    """Config form for the LLM action."""

    model = forms.ChoiceField(
        label=_("Model"),
        choices=_model_choices,
        help_text=_(
            "Models are configured via the AUTOMATION_LLM_MODELS setting "
            '(LiteLLM model strings like "anthropic/claude-opus-4-8" or "openai/gpt-4.1"). '
            "An active API key for the provider must exist under Automations → Secrets."
        ),
    )
    system_prompt = forms.CharField(
        label=_("System prompt"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        validators=[validate_template],
        help_text=_("Optional. Supports {{ dotted.path }} substitution against the automation data."),
    )
    prompt = forms.CharField(
        label=_("Prompt"),
        widget=forms.Textarea(attrs={"rows": 6}),
        validators=[validate_template],
        help_text=_("Supports {{ dotted.path }} substitution against the automation data."),
    )
    output_schema = forms.CharField(
        label=_("Output JSON schema"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        validators=[_validate_json_schema],
        help_text=_(
            "Optional JSON schema. When set, the model's response is constrained to valid JSON "
            "and becomes the new data rows (a list) or a single row (an object)."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        default = getattr(settings, "AUTOMATION_LLM_DEFAULT", None)
        if default:
            self.fields["model"].initial = default


class LLMActionPluginModel(BaseActionPluginModel):
    """Run one LLM completion per run over the automation data.

    Prompts are rendered against the first data row (with all rows exposed
    as ``data``). With an output schema, the parsed JSON becomes the new
    rows; without one, a single ``{"text", "model", "usage"}`` row is
    emitted. Rate limits pause the action (revived by ``runautomations``),
    failing permanently after :data:`MAX_LLM_RETRIES` attempts.
    """

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows: list) -> list:
        config = self.config or {}
        first_row = rows[0] if rows and isinstance(rows[0], dict) else {}
        context = {**first_row, "data": rows}

        schema = config.get("output_schema") or None
        if isinstance(schema, str):
            schema = json.loads(schema) if schema.strip() else None

        prompt = str(safe_render(str(config.get("prompt") or ""), context))
        system = str(safe_render(str(config.get("system_prompt") or ""), context)) or None

        try:
            result = llm.complete(
                model=config.get("model") or "",
                prompt=prompt,
                system=system,
                schema=schema,
            )
        except llm.LLMRateLimited as exc:
            retries = 0
            if isinstance(action.result, dict):
                retries = int(action.result.get("_llm_retries", 0))
            if retries + 1 >= MAX_LLM_RETRIES:
                raise llm.LLMError(f"Rate limited {MAX_LLM_RETRIES} times, giving up: {exc}") from exc
            action.result = {"_llm_retries": retries + 1}
            action.save(update_fields=["result"])
            raise ActionPause(
                until=now() + datetime.timedelta(seconds=exc.retry_after),
                message=f"LLM rate limited, retry {retries + 1}/{MAX_LLM_RETRIES}",
            ) from exc

        if result.json is not None:
            if isinstance(result.json, list):
                return result.json
            return [result.json if isinstance(result.json, dict) else {"value": result.json}]
        return [{"text": result.text, "model": result.model, "usage": result.usage}]

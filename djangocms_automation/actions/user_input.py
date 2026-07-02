"""Human-in-the-loop action: pause the automation until a user resumes it."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from ..instances import WAITING, AutomationAction
from ..models import BaseActionPluginModel
from ..utilities.templates import safe_render, validate_template


class UserInputActionForm(forms.Form):
    """Config form for the user-input action."""

    note = forms.CharField(
        label=_("Note"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        validators=[validate_template],
        help_text=_(
            "Shown to the user who resumes this task. Supports {{ dotted.path }} substitution "
            "against the automation data."
        ),
    )
    permissions = forms.CharField(
        label=_("Required permissions"),
        required=False,
        help_text=_(
            "Comma-separated permissions (app_label.codename) a user needs to resume this task. "
            "Superusers can always resume."
        ),
    )


class UserInputActionPluginModel(BaseActionPluginModel):
    """Pause the automation until a permitted user resumes it.

    The action goes ``WAITING`` with ``requires_interaction`` set; open
    tasks are listed in the admin (Execution Instances → Open tasks) and
    resumed via :func:`djangocms_automation.engine.resume_action`.
    """

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def execute(
        self,
        action: AutomationAction,
        data: list,
        single_step: bool = False,
        plugin_dict: dict | None = None,
    ) -> tuple[str, dict]:
        config = self.config or {}
        action.requires_interaction = True
        action.interaction_permissions = [
            perm.strip() for perm in str(config.get("permissions") or "").split(",") if perm.strip()
        ]
        first_row = data[0] if data and isinstance(data[0], dict) else {}
        note = safe_render(str(config.get("note") or ""), {**first_row, "data": data})
        return WAITING, ({"note": str(note)} if note else {})

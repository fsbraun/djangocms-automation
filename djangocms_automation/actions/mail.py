"""Email action: send one email per data row via Django's email framework."""

from __future__ import annotations

from django.conf import settings
from django.core.mail import EmailMessage

from ..models import BaseActionPluginModel


class MailActionPluginModel(BaseActionPluginModel):
    """Send an email per data row using the configured ``EMAIL_BACKEND``.

    Config fields (from ``MailActionDataForm``): ``subject``,
    ``recipient_email`` and optional ``from_email`` are expressions;
    ``body`` is a template rendered with ``{{ dotted.path }}``
    substitution against the current row.

    Each output row is the input row plus a ``_mail`` entry recording the
    send outcome. If every row fails, the action fails; partial failures
    complete with per-row status.
    """

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows: list) -> list:
        if not rows:
            rows = [{}]
        output = []
        errors = 0
        for row in rows:
            row = row if isinstance(row, dict) else {"value": row}
            mail_status = {"sent": False, "recipient": None, "error": None}
            try:
                inputs = self.resolve_inputs(row, rows)
                recipient = inputs.get("recipient_email")
                if not recipient:
                    raise ValueError("No recipient email resolved")
                message = EmailMessage(
                    subject=str(inputs.get("subject") or ""),
                    body=str(inputs.get("body") or ""),
                    from_email=inputs.get("from_email") or settings.DEFAULT_FROM_EMAIL,
                    to=[str(recipient)],
                )
                message.send(fail_silently=False)
                mail_status["sent"] = True
                mail_status["recipient"] = str(recipient)
            except Exception as exc:  # noqa: BLE001 - recorded per row
                errors += 1
                mail_status["error"] = str(exc)
            output.append({**row, "_mail": mail_status})
        if errors == len(output):
            raise RuntimeError(f"Sending failed for all {errors} recipient(s): {output[0]['_mail']['error']}")
        return output

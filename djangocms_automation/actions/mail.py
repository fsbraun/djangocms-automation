"""Email action: send one email per item via Django's email framework."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.utils.html import strip_tags

from ..models import BaseActionPluginModel

logger = logging.getLogger(__name__)


def _compose(*, subject: str, body: str, html: bool, from_email, recipient: str):
    """The message to send, in one part or two.

    A plain-text alternative always goes with the HTML. Some clients will not
    render markup, some people turn it off, and a mail with no text part
    arrives blank for them — which is worse than the markup they were avoiding.
    It is derived from the HTML rather than asked for separately: an editor
    made to write every message twice writes the second one badly.
    """
    if not html:
        return EmailMessage(subject=subject, body=body, from_email=from_email, to=[recipient])
    message = EmailMultiAlternatives(
        subject=subject,
        body=strip_tags(body),
        from_email=from_email,
        to=[recipient],
    )
    message.attach_alternative(body, "text/html")
    return message


class MailActionPluginModel(BaseActionPluginModel):
    """Send an email per data row using the configured ``EMAIL_BACKEND``.

    Config fields (from ``MailActionDataForm``): ``subject``,
    ``recipient_email`` and optional ``from_email`` are expressions;
    ``body`` is a template rendered with ``{{ dotted.path }}``
    substitution against the current row.

    Returns the named ``delivery`` result. The engine saves it in the selected
    field while preserving the incoming item. Delivery errors fail the action.
    """

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    default_outputs = {"delivery": {"field": "delivery", "mode": "replace"}}

    def perform(self, context, inputs) -> dict:
        recipient = inputs.get("recipient_email")
        if not recipient:
            raise ValueError("No recipient email resolved")
        message = _compose(
            subject=str(inputs.get("subject") or ""),
            body=str(inputs.get("body") or ""),
            html=str(inputs.get("body_format") or "") == "html",
            from_email=inputs.get("from_email") or settings.DEFAULT_FROM_EMAIL,
            recipient=str(recipient),
        )
        sent = message.send(fail_silently=False)
        return {"delivery": {"sent": bool(sent), "recipient": str(recipient)}}

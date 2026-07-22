Receiving webhooks
==================

Automations can be started by inbound HTTP webhooks — from mail providers,
payment services, CI systems, or any service that can POST JSON.

Setup
-----

Include the package URLs in your project urlconf:

.. code-block:: python

    urlpatterns = [
        # ...
        path("automation/", include("djangocms_automation.urls")),
    ]

Each webhook-based trigger carries a secret **token** in its configuration
(auto-generated in the trigger admin; leave the field empty to get a fresh
one). The endpoint is::

    POST https://your-site/automation/webhook/<token>/

Responses: ``200 {"triggered": N, "filtered": M}``, ``404`` for an unknown
token or inactive automation, ``403`` for a failed signature check, and
``400`` for a malformed payload or one that fails the trigger's data
schema.

Generic webhook trigger
-----------------------

Give your automation a trigger of type *Webhook*. Any JSON object (one data
row) or array of objects (multiple rows) posted to the trigger URL becomes
the automation's data:

.. code-block:: bash

    curl -X POST https://your-site/automation/webhook/<token>/ \
         -H "Content-Type: application/json" \
         -d '{"order_id": 42, "customer": "alice@example.com"}'

Signature verification
----------------------

Set a **signing secret** on the trigger to require authenticated requests.
The sender must include an ``X-Automation-Signature`` header containing the
hex HMAC-SHA256 of the raw request body, keyed with the secret:

.. code-block:: bash

    BODY='{"order_id": 42}'
    SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | sed 's/^.* //')
    curl -X POST https://your-site/automation/webhook/<token>/ \
         -H "Content-Type: application/json" \
         -H "X-Automation-Signature: $SIG" \
         -d "$BODY"

Mail ingestion (example implementation)
---------------------------------------

The *Mail* trigger is a webhook trigger specialized for inbound email.
Point your mail provider's inbound-parse or event webhook at the trigger
URL; common provider field names are normalized automatically
(``to``/``To``/``recipient``, ``from``/``From``/``sender``,
``TextBody``/``text``/``body_text``, ``Subject``/``subject``,
``Message-Id``/``message_id``, ...), a missing ``timestamp`` is filled in,
and the status defaults to ``received``.

The trigger's configured filters decide whether the automation actually
starts — filtered-out messages are acknowledged with ``200`` but don't
fire:

- **Recipient filter** — exact (case-insensitive) match on the recipient.
- **Subject contains** — case-insensitive substring match.
- **Status filter** — ``received`` / ``queued`` / ``sent`` / ``bounced`` /
  ``opened``.

Combined with the LLM and flow plugins this enables inbox automations:
*Mail trigger → LLM assessment → If drop / auto-answer (Send Email with the
LLM's draft) / delegate (Wait for User)*.

Writing your own webhook trigger
--------------------------------

Subclass :class:`djangocms_automation.triggers.WebhookTrigger` and register
it. Override :meth:`parse_payload` to adapt a provider's payload shape (and
optionally :meth:`verify_request` for provider-specific signatures — e.g.
Mailgun's timestamp/token scheme or Stripe's ``Stripe-Signature`` header):

.. code-block:: python

    from djangocms_automation.triggers import WebhookTrigger, trigger_registry

    class StripeWebhookTrigger(WebhookTrigger):
        id = "stripe"
        name = "Stripe event"
        description = "Starts on a Stripe webhook event."
        icon = "bi-credit-card"
        data_schema = {}  # optionally constrain the rows

        def parse_payload(self, request, config):
            rows = super().parse_payload(request, config)
            # Unwrap the Stripe envelope; return [] to accept-but-ignore.
            return [row["data"]["object"] for row in rows if row.get("type") == "invoice.paid"]

    trigger_registry.register(StripeWebhookTrigger)

Configuration form fields declared on the class (like the inherited
``token`` and ``signing_secret``) appear in the trigger admin and are
stored in the trigger's config JSON, which is passed to ``parse_payload``
and ``verify_request``.

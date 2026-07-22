"""Tests for the webhook framework: generic webhooks and mail ingestion."""

import hashlib
import hmac
import json

import pytest

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation.instances import COMPLETED, AutomationAction
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
from djangocms_automation.triggers import (
    GenericWebhookTrigger,
    MailTrigger,
    WebhookTrigger,
    generate_webhook_token,
    trigger_registry,
)

TOKEN = "test-webhook-token-123"


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Webhook Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Webhook automation content",
    )


def _make_trigger(automation_content, settings, trigger_type="webhook", config=None):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="start",
        type=trigger_type,
        position=0,
        config={"token": TOKEN, **(config or {})},
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)
    return trigger


def _url(token=TOKEN):
    return reverse("djangocms_automation:webhook", kwargs={"token": token})


# ---------------------------------------------------------------------------
# Framework basics
# ---------------------------------------------------------------------------


def test_generate_webhook_token_is_unique_and_urlsafe():
    tokens = {generate_webhook_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 24 and "/" not in t for t in tokens)


def test_webhook_triggers_registered():
    assert trigger_registry.get("webhook") is GenericWebhookTrigger
    assert issubclass(MailTrigger, WebhookTrigger)


@pytest.mark.django_db
def test_generic_webhook_fires_automation(client, automation_content, settings):
    trigger = _make_trigger(automation_content, settings)

    response = client.post(_url(), data=json.dumps({"order_id": 42}), content_type="application/json")

    assert response.status_code == 200
    assert response.json() == {"triggered": 1, "filtered": 0}
    instance = trigger.automation_content.automationinstance_set.first()
    assert instance is not None
    assert instance.initial_data == [{"order_id": 42}]
    action = AutomationAction.objects.get(automation_instance=instance)
    assert action.state == COMPLETED


@pytest.mark.django_db
def test_generic_webhook_accepts_array_payload(client, automation_content, settings):
    trigger = _make_trigger(automation_content, settings)

    payload = [{"n": 1}, {"n": 2}]
    response = client.post(_url(), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    instance = trigger.automation_content.automationinstance_set.first()
    assert instance.initial_data == payload


@pytest.mark.django_db
def test_unknown_token_404(client, automation_content, settings):
    _make_trigger(automation_content, settings)
    response = client.post(_url("wrong-token"), data="{}", content_type="application/json")
    assert response.status_code == 404


@pytest.mark.django_db
def test_inactive_automation_404(client, automation_content, settings):
    trigger = _make_trigger(automation_content, settings)
    automation = trigger.automation_content.automation
    automation.is_active = False
    automation.save()
    response = client.post(_url(), data="{}", content_type="application/json")
    assert response.status_code == 404


@pytest.mark.django_db
def test_non_webhook_trigger_type_is_not_matched(client, automation_content, settings):
    # A click trigger holding a token in config must not be reachable.
    _make_trigger(automation_content, settings, trigger_type="click")
    response = client.post(_url(), data="{}", content_type="application/json")
    assert response.status_code == 404


@pytest.mark.django_db
def test_get_method_not_allowed(client, automation_content, settings):
    _make_trigger(automation_content, settings)
    response = client.get(_url())
    assert response.status_code == 405


@pytest.mark.django_db
def test_malformed_payload_400(client, automation_content, settings):
    trigger = _make_trigger(automation_content, settings)
    for body in ["not json", '"a string"', "[1, 2]"]:
        response = client.post(_url(), data=body, content_type="application/json")
        assert response.status_code == 400
    assert trigger.automation_content.automationinstance_set.count() == 0


# ---------------------------------------------------------------------------
# HMAC signature verification
# ---------------------------------------------------------------------------


def _sign(secret, body):
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


@pytest.mark.django_db
def test_signature_required_when_secret_configured(client, automation_content, settings):
    trigger = _make_trigger(automation_content, settings, config={"signing_secret": "s3cret"})
    body = json.dumps({"x": 1})

    # No signature -> rejected.
    response = client.post(_url(), data=body, content_type="application/json")
    assert response.status_code == 403

    # Wrong signature -> rejected.
    response = client.post(
        _url(), data=body, content_type="application/json", headers={"X-Automation-Signature": "bad"}
    )
    assert response.status_code == 403
    assert trigger.automation_content.automationinstance_set.count() == 0

    # Correct signature -> fires.
    response = client.post(
        _url(),
        data=body,
        content_type="application/json",
        headers={"X-Automation-Signature": _sign("s3cret", body)},
    )
    assert response.status_code == 200
    assert trigger.automation_content.automationinstance_set.count() == 1


# ---------------------------------------------------------------------------
# Mail ingestion (example implementation)
# ---------------------------------------------------------------------------


MAIL_PAYLOAD = {
    "message_id": "<abc@mail.example.com>",
    "to": "support@example.com",
    "from": "customer@example.org",
    "Subject": "Invoice overdue",
    "TextBody": "Hello, my invoice is overdue.",
}


@pytest.mark.django_db
def test_mail_webhook_normalizes_provider_aliases(client, automation_content, settings):
    trigger = _make_trigger(automation_content, settings, trigger_type="mail")

    response = client.post(_url(), data=json.dumps(MAIL_PAYLOAD), content_type="application/json")

    assert response.status_code == 200
    assert response.json()["triggered"] == 1
    row = trigger.automation_content.automationinstance_set.first().initial_data[0]
    assert row["recipient"] == "support@example.com"
    assert row["sender"] == "customer@example.org"
    assert row["subject"] == "Invoice overdue"
    assert row["body_text"] == "Hello, my invoice is overdue."
    assert row["status"] == "received"
    assert row["timestamp"]  # filled when missing
    # Original provider keys are preserved.
    assert row["TextBody"] == "Hello, my invoice is overdue."


@pytest.mark.django_db
@pytest.mark.parametrize(
    "config, expected_triggered, expected_filtered",
    [
        ({"recipient_filter": "support@example.com"}, 1, 0),
        ({"recipient_filter": "other@example.com"}, 0, 1),
        ({"subject_contains": "invoice"}, 1, 0),  # case-insensitive
        ({"subject_contains": "refund"}, 0, 1),
        ({"status_filter": "received"}, 1, 0),
        ({"status_filter": "bounced"}, 0, 1),
    ],
)
def test_mail_webhook_filters(client, automation_content, settings, config, expected_triggered, expected_filtered):
    trigger = _make_trigger(automation_content, settings, trigger_type="mail", config=config)

    response = client.post(_url(), data=json.dumps(MAIL_PAYLOAD), content_type="application/json")

    assert response.status_code == 200
    assert response.json() == {"triggered": expected_triggered, "filtered": expected_filtered}
    assert trigger.automation_content.automationinstance_set.count() == expected_triggered


@pytest.mark.django_db
def test_mail_webhook_schema_validation_400(client, automation_content, settings):
    trigger = _make_trigger(automation_content, settings, trigger_type="mail")
    # No message_id and no recipient alias -> normalized row misses required fields.
    response = client.post(_url(), data=json.dumps({"Subject": "hi"}), content_type="application/json")
    assert response.status_code == 400
    assert trigger.automation_content.automationinstance_set.count() == 0


@pytest.mark.django_db
def test_admin_form_autogenerates_token_for_webhook_triggers(automation_content, rf, admin_user):
    from django.contrib.admin.sites import AdminSite

    from djangocms_automation.admin import AutomationTriggerAdmin

    instance = AutomationTrigger(automation_content=automation_content, type="webhook", slot="hook", position=0)
    admin = AutomationTriggerAdmin(AutomationTrigger, AdminSite())
    request = rf.get("/", {"type": "webhook"})
    request.user = admin_user
    form_class = admin.get_form(request, instance)

    form = form_class(
        data={
            "automation_content": automation_content.pk,
            "type": "webhook",
            "slot": "hook",
            "position": 0,
            "token": "",  # left empty -> auto-generated
        },
        instance=instance,
    )
    assert form.is_valid(), form.errors
    saved = form.save(commit=True)
    assert saved.config["token"]
    assert len(saved.config["token"]) >= 24

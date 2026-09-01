"""End-to-end tests for the MailAction plugin."""

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from django.contrib.contenttypes.models import ContentType
from django.core import mail

from djangocms_automation.actions.mail import MailActionPluginModel
from djangocms_automation.instances import COMPLETED, FAILED, AutomationAction
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Mail Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Mail automation content",
    )


@pytest.fixture
def mail_setup(automation_content, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    settings.DEFAULT_FROM_EMAIL = "noreply@example.com"

    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="start",
        type="click",
        position=0,
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    return trigger, placeholder


@pytest.mark.django_db
def test_mail_action_sends_per_row(mail_setup, settings):
    trigger, placeholder = mail_setup
    plugin = add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=settings.LANGUAGE_CODE,
    )
    model = MailActionPluginModel.objects.get(pk=plugin.pk)
    model.config = {
        "subject": "'Welcome'",
        "body": "Hello {{ name }}, your order is {{ order_id }}.",
        "recipient_email": "email",
        "from_email": "",
    }
    model.save()

    trigger.trigger_execution(
        data=[
            {"name": "Alice", "email": "alice@example.com", "order_id": 1},
            {"name": "Bob", "email": "bob@example.com", "order_id": 2},
        ],
        start=True,
    )

    assert len(mail.outbox) == 2
    assert mail.outbox[0].subject == "Welcome"
    assert mail.outbox[0].to == ["alice@example.com"]
    assert mail.outbox[0].body == "Hello Alice, your order is 1."
    assert mail.outbox[0].from_email == "noreply@example.com"
    assert mail.outbox[1].to == ["bob@example.com"]

    instance = trigger.automation_content.automationinstance_set.first()
    action = AutomationAction.objects.get(automation_instance=instance)
    assert action.state == COMPLETED
    # Output rows carry the per-row mail status.
    assert all(row["_mail"]["sent"] for row in action.result)
    assert instance.status == COMPLETED


@pytest.mark.django_db
def test_mail_action_partial_failure_completes(mail_setup, settings):
    trigger, placeholder = mail_setup
    plugin = add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=settings.LANGUAGE_CODE,
    )
    model = MailActionPluginModel.objects.get(pk=plugin.pk)
    model.config = {
        "subject": "'Hi'",
        "body": "Hi {{ name }}",
        "recipient_email": "email",
    }
    model.save()

    trigger.trigger_execution(
        data=[
            {"name": "NoMail"},  # no email key -> row fails
            {"name": "Bob", "email": "bob@example.com"},
        ],
        start=True,
    )

    assert len(mail.outbox) == 1
    instance = trigger.automation_content.automationinstance_set.first()
    action = AutomationAction.objects.get(automation_instance=instance)
    assert action.state == COMPLETED
    statuses = [row["_mail"]["sent"] for row in action.result]
    assert statuses == [False, True]


@pytest.mark.django_db
def test_mail_action_total_failure_fails_action_and_instance(mail_setup, settings):
    trigger, placeholder = mail_setup
    plugin = add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=settings.LANGUAGE_CODE,
    )
    model = MailActionPluginModel.objects.get(pk=plugin.pk)
    model.config = {
        "subject": "'Hi'",
        "body": "Hi",
        "recipient_email": "email",  # never resolvable
    }
    model.save()

    trigger.trigger_execution(data=[{"name": "NoMail"}], start=True)

    assert len(mail.outbox) == 0
    instance = trigger.automation_content.automationinstance_set.first()
    action = AutomationAction.objects.get(automation_instance=instance)
    assert action.state == FAILED
    instance.refresh_from_db()
    assert instance.status == FAILED
    assert instance.finished is not None


def _send(placeholder, settings, config):
    """One Send Email step, configured and run through the engine."""
    plugin = add_plugin(placeholder=placeholder, plugin_type="MailAction", language=settings.LANGUAGE_CODE)
    model = MailActionPluginModel.objects.get(pk=plugin.pk)
    model.config = config
    model.save()


@pytest.mark.django_db
def test_an_html_body_arrives_in_both_forms(mail_setup, settings):
    """A mail with no text part arrives blank for anyone whose client will not
    render markup, which is worse than the markup they were avoiding."""
    trigger, placeholder = mail_setup
    _send(
        placeholder,
        settings,
        {
            "recipient_email": "'to@example.com'",
            "subject": "'Digest'",
            "body": "<p>Hello <b>Ada</b></p>",
            "body_format": "'html'",
        },
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    sent = mail.outbox[-1]
    assert sent.body == "Hello Ada", "the text part, derived rather than asked for twice"
    assert sent.alternatives[0][0] == "<p>Hello <b>Ada</b></p>"
    assert sent.alternatives[0][1] == "text/html"


@pytest.mark.django_db
def test_a_plain_body_stays_one_part(mail_setup, settings):
    """The default is unchanged: no alternative, nothing stripped."""
    trigger, placeholder = mail_setup
    _send(
        placeholder,
        settings,
        {"recipient_email": "'to@example.com'", "subject": "'Digest'", "body": "Hello <not markup>"},
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    sent = mail.outbox[-1]
    assert sent.body == "Hello <not markup>"
    assert not getattr(sent, "alternatives", [])

"""The dummy model — a provider stand-in for building an AI step.

Its value is that an editor can get a whole step working, tools and approvals
included, before choosing a provider or paying anyone. So what is tested here is
that a run through it looks like a real one.
"""

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from django.contrib.contenttypes.models import ContentType

from djangocms_automation.ai import dummy, llm
from djangocms_automation.instances import COMPLETED, FAILED, WAITING, AutomationAction
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def run_setup(db, admin_user, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    settings.AUTOMATION_LLM_MODELS = ["dummy/echo"]
    automation = Automation.objects.create(name="Dummy", is_active=True)
    content = AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Dummy")
    trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click", position=0)
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=content.pk,
        slot="start",
    )[0]
    return trigger, placeholder


def add_step(placeholder, settings, prompt):
    return add_plugin(
        placeholder=placeholder,
        plugin_type="AIStep",
        language=settings.LANGUAGE_CODE,
        config={"model": "dummy/echo", "prompt": prompt},
    )


def step_action():
    return AutomationAction.objects.filter(parent__isnull=True).order_by("id").last()


def test_only_a_dummy_string_is_answered_locally():
    assert dummy.is_dummy("dummy/echo")
    assert not dummy.is_dummy("anthropic/claude-opus-4-8")


@pytest.mark.django_db
def test_it_is_still_gated_by_the_allowlist(settings):
    """Opt-in works the same way as for a real model: nothing runs unless the
    project said it could."""
    settings.AUTOMATION_LLM_MODELS = []
    with pytest.raises(llm.LLMError, match="not allowed"):
        llm.complete(model="dummy/echo", prompt="hello")


@pytest.mark.django_db
def test_it_needs_no_api_key(run_setup, settings):
    """The point of it: a step runs before anybody has an account anywhere."""
    from djangocms_automation.models import APIKey

    assert not APIKey.objects.exists()
    trigger, placeholder = run_setup
    add_step(placeholder, settings, "Summarise {{ seed }}.")

    trigger.trigger_execution(data=[{"seed": "the thing"}])

    action = step_action()
    assert action.state == COMPLETED
    assert action.result[0]["text"] == "Summarise the thing."


@pytest.mark.django_db
def test_a_directive_makes_it_call_a_tool(run_setup, settings):
    """The whole loop — dispatch, run, observe, answer — without a provider."""
    from django.core import mail

    trigger, placeholder = run_setup
    step = add_step(
        placeholder,
        settings,
        'Reply to them.\n!call reply {"subject": "Thanks", "body": "We got it."}',
    )
    add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=settings.LANGUAGE_CODE,
        target=step,
        tool_name="reply",
        tool_description="Reply to the customer.",
        exposed_fields=["subject", "body"],
        requires_approval=False,
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    assert mail.outbox[-1].subject == "Thanks"
    action = step_action()
    assert action.state == COMPLETED
    assert "Tools returned" in action.result[0]["text"], "it reports what it saw"


@pytest.mark.django_db
def test_a_directive_reaches_the_approval_gate(run_setup, settings):
    """Which is the thing worth trying before a provider is involved."""
    trigger, placeholder = run_setup
    step = add_step(placeholder, settings, '!call reply {"subject": "Thanks"}')
    add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=settings.LANGUAGE_CODE,
        target=step,
        tool_name="reply",
        tool_description="Reply.",
        exposed_fields=["subject"],
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    call = AutomationAction.objects.exclude(parent__isnull=True).latest("id")
    assert call.state == WAITING and call.requires_interaction
    assert call.result["arguments"] == {"subject": "Thanks"}


@pytest.mark.django_db
def test_it_can_answer_in_a_shape(run_setup, settings):
    trigger, placeholder = run_setup
    add_step(placeholder, settings, '!json {"topic": "billing"}')

    trigger.trigger_execution(data=[{"seed": 1}])

    assert step_action().result == [{"topic": "billing"}]


@pytest.mark.django_db
def test_it_can_be_told_to_fail(run_setup, settings):
    """So that what a step does with a broken provider is testable too."""
    trigger, placeholder = run_setup
    add_step(placeholder, settings, "!fail the provider is down")

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.state == FAILED
    assert "the provider is down" in action.result["error"]


@pytest.mark.django_db
def test_an_unknown_tool_name_is_corrected_as_a_real_one_would_be(run_setup, settings):
    trigger, placeholder = run_setup
    step = add_step(placeholder, settings, "!call nope {}")
    add_plugin(
        placeholder=placeholder,
        plugin_type="ActionPlugin",
        language=settings.LANGUAGE_CODE,
        target=step,
        tool_name="echo",
        tool_description="Echo.",
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    from djangocms_automation.ai.state import AgentState

    action = step_action()
    observations = [m for m in AgentState.load(action).messages if m.get("role") == "tool"]
    assert "echo" in observations[0]["content"]


def test_allowing_a_model_gives_its_provider_somewhere_to_keep_a_key(settings):
    """Two lists that must agree, with nothing keeping them in step.

    A key is looked up by the provider prefix of the model string; the admin
    offers whatever the service registry holds. A project could allow
    ``deepseek/deepseek-chat``, find no way to store a ``deepseek`` key, and
    learn why only when a run failed for want of one.
    """
    from djangocms_automation.ai.llm import register_llm_services
    from djangocms_automation.models import APIKey
    from djangocms_automation.services import service_registry

    settings.AUTOMATION_LLM_MODELS = [("deepseek/deepseek-chat", "DeepSeek Chat")]
    try:
        assert register_llm_services() == ["deepseek"]
        assert ("deepseek", "deepseek") in APIKey.get_service_choices(), "and so the admin offers it"
    finally:
        service_registry.unregister("deepseek")


def test_a_provider_that_is_already_named_keeps_its_name(settings):
    """``openai`` reads *OpenAI*, and allowing a model does not undo that."""
    from djangocms_automation.ai.llm import register_llm_services
    from djangocms_automation.models import APIKey

    settings.AUTOMATION_LLM_MODELS = [("openai/gpt-4.1", "GPT-4.1")]

    assert register_llm_services() == [], "nothing to add"
    assert ("openai", "OpenAI") in APIKey.get_service_choices()


def test_the_local_model_needs_no_secret(settings):
    """There is no provider and no key, so offering a place for one misleads."""
    from djangocms_automation.ai.llm import register_llm_services
    from djangocms_automation.services import service_registry

    settings.AUTOMATION_LLM_MODELS = [("dummy/echo", "Echo")]

    assert register_llm_services() == []
    assert service_registry.get("dummy") is None

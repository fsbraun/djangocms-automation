"""Tests for the provider-independent LLM layer and the LLM action.

LiteLLM is stubbed out — no network access and no hard dependency on the
``litellm`` package in the test environment.
"""

import json
import types
from unittest import mock

import pytest

from django.contrib.contenttypes.models import ContentType

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation import llm
from djangocms_automation.actions.llm_action import LLMActionPluginModel
from djangocms_automation.instances import COMPLETED, FAILED, PENDING, AutomationAction
from djangocms_automation.models import APIKey, Automation, AutomationContent, AutomationTrigger


class FakeRateLimitError(Exception):
    def __init__(self, message="rate limited"):
        super().__init__(message)
        self.response = types.SimpleNamespace(headers={"retry-after": "30"})


class FakeAPIConnectionError(Exception):
    pass


def make_fake_litellm(completion):
    fake = types.SimpleNamespace()
    fake.completion = completion
    fake.RateLimitError = FakeRateLimitError
    fake.APIConnectionError = FakeAPIConnectionError
    return fake


def make_response(content, model="anthropic/claude-opus-4-8"):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))],
        model=model,
        usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


@pytest.fixture
def api_key(db):
    return APIKey.objects.create(name="Anthropic key", service="anthropic", api_key="sk-test-123")


@pytest.fixture
def llm_settings(settings):
    settings.AUTOMATION_LLM_MODELS = ["anthropic/claude-opus-4-8", "openai/gpt-4.1"]
    return settings


# ---------------------------------------------------------------------------
# llm.complete()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_complete_passes_model_key_and_messages(llm_settings, api_key):
    calls = {}

    def completion(**kwargs):
        calls.update(kwargs)
        return make_response("Hello!")

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        result = llm.complete(
            model="anthropic/claude-opus-4-8",
            prompt="Say hello",
            system="Be nice",
        )

    assert calls["model"] == "anthropic/claude-opus-4-8"
    assert calls["api_key"] == "sk-test-123"
    assert calls["messages"] == [
        {"role": "system", "content": "Be nice"},
        {"role": "user", "content": "Say hello"},
    ]
    assert "response_format" not in calls
    assert result.text == "Hello!"
    assert result.json is None
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}


@pytest.mark.django_db
def test_complete_with_schema_sets_response_format_and_parses(llm_settings, api_key):
    calls = {}
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "additionalProperties": False}

    def completion(**kwargs):
        calls.update(kwargs)
        return make_response('{"x": 5}')

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        result = llm.complete(model="anthropic/claude-opus-4-8", prompt="p", schema=schema)

    assert calls["response_format"]["type"] == "json_schema"
    assert calls["response_format"]["json_schema"]["schema"] == schema
    assert result.json == {"x": 5}


@pytest.mark.django_db
def test_complete_rejects_unlisted_model(llm_settings, api_key):
    with pytest.raises(llm.LLMError, match="not allowed"):
        llm.complete(model="anthropic/claude-haiku-4-5", prompt="p")


@pytest.mark.django_db
def test_complete_requires_api_key(llm_settings):
    with pytest.raises(llm.LLMError, match="No active API key"):
        llm.complete(model="anthropic/claude-opus-4-8", prompt="p")


@pytest.mark.django_db
def test_complete_maps_rate_limit(llm_settings, api_key):
    def completion(**kwargs):
        raise FakeRateLimitError()

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        with pytest.raises(llm.LLMRateLimited) as excinfo:
            llm.complete(model="anthropic/claude-opus-4-8", prompt="p")
    assert excinfo.value.retry_after == 30


@pytest.mark.django_db
def test_complete_maps_generic_errors(llm_settings, api_key):
    def completion(**kwargs):
        raise RuntimeError("boom")

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        with pytest.raises(llm.LLMError, match="boom"):
            llm.complete(model="anthropic/claude-opus-4-8", prompt="p")


# ---------------------------------------------------------------------------
# LLM action end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="LLM Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="LLM automation content",
    )


@pytest.fixture
def run_setup(automation_content, llm_settings, api_key):
    llm_settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
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


def _add_llm_action(placeholder, config, settings):
    plugin = add_plugin(placeholder=placeholder, plugin_type="LLMAction", language=settings.LANGUAGE_CODE)
    model = LLMActionPluginModel.objects.get(pk=plugin.pk)
    model.config = config
    model.save()
    return model


@pytest.mark.django_db
def test_llm_action_renders_prompt_and_outputs_text_row(run_setup, llm_settings):
    trigger, placeholder = run_setup
    _add_llm_action(
        placeholder,
        {
            "model": "anthropic/claude-opus-4-8",
            "system_prompt": "You classify sentiment.",
            "prompt": "Classify: {{ text }}",
            "output_schema": "",
        },
        llm_settings,
    )
    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        return make_response("positive")

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        trigger.trigger_execution(data=[{"text": "I love this"}], start=True)

    assert seen["messages"][1]["content"] == "Classify: I love this"
    action = AutomationAction.objects.get(
        automation_instance=trigger.automation_content.automationinstance_set.first()
    )
    assert action.state == COMPLETED
    assert action.result[0]["text"] == "positive"
    assert action.result[0]["usage"]["input_tokens"] == 10


@pytest.mark.django_db
def test_llm_action_schema_list_becomes_rows(run_setup, llm_settings):
    trigger, placeholder = run_setup
    schema = {"type": "array", "items": {"type": "object", "additionalProperties": False}}
    _add_llm_action(
        placeholder,
        {
            "model": "anthropic/claude-opus-4-8",
            "prompt": "Extract items",
            "output_schema": json.dumps(schema),
        },
        llm_settings,
    )

    def completion(**kwargs):
        return make_response('[{"sku": "A"}, {"sku": "B"}]')

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        trigger.trigger_execution(data=[], start=True)

    action = AutomationAction.objects.get(
        automation_instance=trigger.automation_content.automationinstance_set.first()
    )
    assert action.state == COMPLETED
    assert action.result == [{"sku": "A"}, {"sku": "B"}]


@pytest.mark.django_db
def test_llm_action_rate_limit_pauses_action(run_setup, llm_settings):
    trigger, placeholder = run_setup
    _add_llm_action(
        placeholder,
        {"model": "anthropic/claude-opus-4-8", "prompt": "p"},
        llm_settings,
    )

    def completion(**kwargs):
        raise FakeRateLimitError()

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        trigger.trigger_execution(data=[], start=True)

    action = AutomationAction.objects.get(
        automation_instance=trigger.automation_content.automationinstance_set.first()
    )
    assert action.state == PENDING
    assert action.paused_until is not None
    assert action.result["_llm_retries"] == 1
    assert "rate limited" in action.message.lower()


@pytest.mark.django_db
def test_llm_action_api_error_fails_action(run_setup, llm_settings):
    trigger, placeholder = run_setup
    _add_llm_action(
        placeholder,
        {"model": "anthropic/claude-opus-4-8", "prompt": "p"},
        llm_settings,
    )

    def completion(**kwargs):
        raise RuntimeError("provider exploded")

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        trigger.trigger_execution(data=[], start=True)

    instance = trigger.automation_content.automationinstance_set.first()
    action = AutomationAction.objects.get(automation_instance=instance)
    assert action.state == FAILED
    assert "provider exploded" in action.result["error"]
    instance.refresh_from_db()
    assert instance.status == FAILED


@pytest.mark.django_db
def test_llm_action_gives_up_after_max_retries(run_setup, llm_settings):
    from djangocms_automation.actions.llm_action import MAX_LLM_RETRIES

    trigger, placeholder = run_setup
    _add_llm_action(
        placeholder,
        {"model": "anthropic/claude-opus-4-8", "prompt": "p"},
        llm_settings,
    )

    def completion(**kwargs):
        raise FakeRateLimitError()

    with mock.patch.object(llm, "_get_litellm", return_value=make_fake_litellm(completion)):
        trigger.trigger_execution(data=[], start=True)
        instance = trigger.automation_content.automationinstance_set.first()
        action = AutomationAction.objects.get(automation_instance=instance)
        # Re-run the paused action until the retry budget is exhausted.
        for _i in range(MAX_LLM_RETRIES):
            from djangocms_automation import engine

            AutomationAction.objects.filter(pk=action.pk).update(paused_until=None)
            engine.run_action(action.pk)
            action.refresh_from_db()
            if action.state == FAILED:
                break

    assert action.state == FAILED
    assert "giving up" in action.result["error"].lower()


# ---------------------------------------------------------------------------
# LLMActionForm validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_llm_form_valid_and_default_model(llm_settings):
    from djangocms_automation.actions.llm_action import LLMActionForm

    llm_settings.AUTOMATION_LLM_DEFAULT = "openai/gpt-4.1"
    form = LLMActionForm(
        data={
            "model": "anthropic/claude-opus-4-8",
            "system_prompt": "Be brief.",
            "prompt": "Summarize {{ text }}",
            "output_schema": '{"type": "object", "additionalProperties": false}',
        }
    )
    assert form.fields["model"].initial == "openai/gpt-4.1"
    assert form.is_valid(), form.errors


@pytest.mark.django_db
@pytest.mark.parametrize(
    "schema, error_fragment",
    [
        ("{not json", "Invalid JSON"),
        ('["a", "b"]', "must be a JSON object"),
        ('{"type": "object"}', "additionalProperties"),
    ],
)
def test_llm_form_rejects_bad_schema(llm_settings, schema, error_fragment):
    from djangocms_automation.actions.llm_action import LLMActionForm

    form = LLMActionForm(data={"model": "anthropic/claude-opus-4-8", "prompt": "p", "output_schema": schema})
    assert not form.is_valid()
    assert error_fragment in str(form.errors["output_schema"])


@pytest.mark.django_db
def test_llm_form_rejects_unlisted_model(llm_settings):
    from djangocms_automation.actions.llm_action import LLMActionForm

    form = LLMActionForm(data={"model": "anthropic/claude-haiku-4-5", "prompt": "p"})
    assert not form.is_valid()
    assert "model" in form.errors


@pytest.mark.django_db
def test_llm_form_rejects_malformed_prompt_template(llm_settings):
    from djangocms_automation.actions.llm_action import LLMActionForm

    form = LLMActionForm(data={"model": "anthropic/claude-opus-4-8", "prompt": "Broken {{ unclosed"})
    assert not form.is_valid()
    assert "prompt" in form.errors

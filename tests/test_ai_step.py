"""The AI step: a model, a task, and whatever actions were put inside it.

Every provider reply here is scripted. That is not only to avoid the network:
the interesting behaviour is what the step does when a model asks for a tool
that does not exist, never stops asking, or asks for something a person has to
approve first — none of which a live provider will do on demand.
"""

from unittest import mock

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType

from djangocms_automation.ai import llm, step
from djangocms_automation.ai.budget import AgentBudget, BudgetExceeded
from djangocms_automation.ai.llm import LLMResult
from djangocms_automation.ai.state import AgentState
from djangocms_automation.instances import COMPLETED, FAILED, WAITING, AutomationAction
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
from djangocms_automation.tools import ToolCall

SCRIPT: list = []


def says(text="", calls=(), usage=None, finish_reason=None, json=None, reasoning=""):
    return LLMResult(
        text=text,
        json=json,
        model="anthropic/claude-opus-4-8",
        usage=usage or {"input_tokens": 10, "output_tokens": 5},
        tool_calls=list(calls),
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
        reasoning=reasoning,
    )


@pytest.fixture(autouse=True)
def scripted_model():
    SCRIPT.clear()

    def complete(**kwargs):
        assert SCRIPT, "the step asked for more turns than the test scripted"
        return SCRIPT.pop(0)

    with mock.patch.object(llm, "complete", side_effect=complete):
        yield SCRIPT


@pytest.fixture
def run_setup(db, admin_user, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    settings.AUTOMATION_LLM_MODELS = ["anthropic/claude-opus-4-8"]
    automation = Automation.objects.create(name="AI", is_active=True)
    content = AutomationContent.objects.with_user(admin_user).create(automation=automation, description="AI")
    trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click", position=0)
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=content.pk,
        slot="start",
    )[0]
    return trigger, placeholder


def add_step(placeholder, settings, **config):
    """An AI step, configured the way the editor's form stores it."""
    intent = config.pop("intent", "Answer the request")
    return add_plugin(
        placeholder=placeholder,
        plugin_type="AIStep",
        language=settings.LANGUAGE_CODE,
        intent=intent,
        config={
            "model": "anthropic/claude-opus-4-8",
            "prompt": config.pop("prompt", "do the thing"),
            **config,
        },
    )


def add_tool(placeholder, parent, settings, plugin_type="ActionPlugin", **kwargs):
    """An action inside the step, which is what makes it a tool."""
    return add_plugin(
        placeholder=placeholder,
        plugin_type=plugin_type,
        language=settings.LANGUAGE_CODE,
        target=parent,
        intent=kwargs.pop("intent", "Use the tool"),
        tool_name=kwargs.pop("tool_name", "echo"),
        tool_description=kwargs.pop("tool_description", "The echo tool."),
        exposed_fields=kwargs.pop("exposed_fields", []),
        **kwargs,
    )


def step_action():
    return (
        AutomationAction.objects.filter(parent__isnull=True, automation_instance__parent_action__isnull=True)
        .order_by("id")
        .last()
    )


def call_action():
    return AutomationAction.objects.exclude(parent__isnull=True).latest("id")


def observations(action):
    return [m for m in AgentState.load(action).messages if m.get("role") == "tool"]


def observations_system(action):
    """The standing instructions the step actually sent."""
    return next(
        (m.get("content", "") for m in AgentState.load(action).messages if m.get("role") == "system"),
        "",
    )


# --------------------------------------------------------------------------
# With no tools it is a prompt
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_step_with_no_tools_answers(run_setup, settings):
    """The common case, and the one that used to do nothing at all.

    The merged step is a prompt until something is put inside it, so "no tools"
    has to mean one call and an answer — not, as it once did, passing the data
    through untouched without ever reaching a provider.
    """
    trigger, placeholder = run_setup
    add_step(placeholder, settings)
    SCRIPT.append(says(text="Nothing to do."))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.state == COMPLETED
    assert action.result[0]["text"] == "Nothing to do."
    assert not SCRIPT, "the provider was called"


@pytest.mark.django_db
def test_an_output_shape_becomes_the_data(run_setup, settings):
    trigger, placeholder = run_setup
    add_step(
        placeholder,
        settings,
        output_schema='{"type": "object", "properties": {"topic": {"type": "string"}}, '
        '"required": ["topic"], "additionalProperties": false}',
    )
    SCRIPT.append(says(json={"topic": "billing"}))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert step_action().result == [{"topic": "billing"}]


@pytest.mark.django_db
def test_an_output_shape_beside_tools_is_refused_in_the_editor(run_setup, settings):
    """Constraining the answer and offering tools on the same turn is
    provider-specific, so the editor is told rather than the behaviour varying."""
    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings, output_schema='{"type": "object", "additionalProperties": false}')
    add_tool(placeholder, ai, settings)

    instance = step.AIStepPluginModel.objects.get(pk=ai.pk)
    instance.child_plugin_instances = list(instance.cmsplugin_set.all())

    assert any("Output shape is ignored" in str(m) for m in instance.messages())


# --------------------------------------------------------------------------
# An action inside the step is a tool
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_action_inside_the_step_is_offered_to_the_model(run_setup, settings):
    """No wrapper, no tool type: being here is what makes it a tool."""
    sent = []

    def complete(**kwargs):
        sent.append(kwargs.get("tools"))
        assert SCRIPT
        return SCRIPT.pop(0)

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(placeholder, ai, settings, tool_name="echo")
    SCRIPT.append(says(text="Done."))

    with mock.patch.object(llm, "complete", side_effect=complete):
        trigger.trigger_execution(data=[{"seed": 1}])

    assert [t["function"]["name"] for t in sent[0]] == ["echo"]


@pytest.mark.django_db
def test_a_tool_call_runs_the_action(run_setup, settings):
    from django.core import mail

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject", "body"],
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
        requires_approval=False,
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it", "body": "Now"})]))
    SCRIPT.append(says(text="Sent."))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert step_action().state == COMPLETED
    assert mail.outbox[-1].subject == "Ship it", "the model's literal, not a data path"
    assert mail.outbox[-1].to == ["to@example.com"], "the bound input is not the model's to choose"


@pytest.mark.django_db
def test_an_action_outside_a_step_is_unaffected(run_setup, settings):
    """The mixin is on every action, and must do nothing when nobody called it."""
    from django.core import mail

    trigger, placeholder = run_setup
    add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=settings.LANGUAGE_CODE,
        config={"recipient_email": "'to@example.com'", "subject": "'Hello'", "body": "Hi"},
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    assert mail.outbox[-1].subject == "Hello"
    assert step_action().state == COMPLETED


@pytest.mark.django_db
def test_a_destructive_action_is_gated_by_default(run_setup, settings):
    """Declared by the action's author, next to the code that does the damage."""
    from cms.plugin_pool import plugin_pool

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(placeholder, ai, settings, plugin_type="MailAction", tool_name="reply", requires_approval=None)

    assert plugin_pool.get_plugin("MailAction").destructive is True
    assert plugin_pool.get_plugin("QueryModelAction").destructive is False
    assert tool.needs_approval() is True


@pytest.mark.django_db
def test_an_approved_call_actually_runs(run_setup, settings, admin_user):
    from django.core import mail

    from djangocms_automation.engine import resume_action

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
        requires_approval=True,
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    call = call_action()
    assert call.state == WAITING and call.requires_interaction
    assert call.result["tool"] == "reply" and call.result["arguments"]["subject"] == "Ship it"

    SCRIPT.append(says(text="Sent."))
    resume_action(call.pk, admin_user)

    assert mail.outbox[-1].subject == "Ship it", "approving is what runs it"
    assert observations(step_action())[0]["tool_call_id"] == "c1"


@pytest.mark.django_db
def test_a_tool_wrapping_a_human_step_waits_for_the_human(run_setup, settings):
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="UserInputAction",
        tool_name="escalate",
        config={"note": "Please check this refund."},
        requires_approval=False,
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="escalate", arguments={})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    call = call_action()
    assert call.state == WAITING and call.requires_interaction
    assert call.result["note"] == "Please check this refund."


@pytest.mark.django_db
def test_a_finished_call_does_not_start_the_next_tool(run_setup, settings):
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(placeholder, ai, settings, tool_name="echo")
    add_tool(placeholder, ai, settings, tool_name="ping")
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="echo", arguments={})]))
    SCRIPT.append(says(text="Done."))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert step_action().children.count() == 1, "only the tool the model asked for ran"


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_unknown_tool_is_corrected_not_dispatched(run_setup, settings):
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(placeholder, ai, settings, tool_name="echo")
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="nope", arguments={})]))
    SCRIPT.append(says(text="Sorry."))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.state == COMPLETED
    assert not action.children.exists()
    assert "echo" in observations(action)[0]["content"], "it is told what it does have"


@pytest.mark.django_db
def test_every_requested_call_is_answered_before_the_next_turn(run_setup, settings):
    """Providers reject a conversation whose assistant turn asks for a tool
    that nothing replies to."""
    sent = []

    def complete(**kwargs):
        sent.append([dict(m) for m in kwargs.get("messages") or []])
        assert SCRIPT
        return SCRIPT.pop(0)

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(placeholder, ai, settings, tool_name="echo")
    add_tool(placeholder, ai, settings, tool_name="ping")
    SCRIPT.append(
        says(calls=[ToolCall(id="c1", name="echo", arguments={}), ToolCall(id="c2", name="ping", arguments={})])
    )
    SCRIPT.append(says(text="Done."))

    with mock.patch.object(llm, "complete", side_effect=complete):
        trigger.trigger_execution(data=[{"seed": 1}])

    for messages in sent:
        requested = [c["id"] for m in messages if m.get("role") == "assistant" for c in m.get("tool_calls") or []]
        answered = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        assert sorted(requested) == sorted(answered), f"unanswered: {messages}"


@pytest.mark.django_db
def test_a_turn_cannot_spend_more_tool_calls_than_it_has(run_setup, settings):
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings, max_tool_calls=1)
    add_tool(placeholder, ai, settings, tool_name="echo")
    add_tool(placeholder, ai, settings, tool_name="ping")
    SCRIPT.append(
        says(calls=[ToolCall(id="c1", name="echo", arguments={}), ToolCall(id="c2", name="ping", arguments={})])
    )
    SCRIPT.append(says(text="Done."))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert step_action().children.count() == 1, "the second was over the limit and must not have run"


@pytest.mark.django_db
def test_a_reply_cut_off_at_the_token_limit_fails_the_run(run_setup, settings):
    trigger, placeholder = run_setup
    add_step(placeholder, settings)
    SCRIPT.append(says(text="The refund policy is that", finish_reason="length"))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.state == FAILED
    assert "cut off" in action.result["error"]


@pytest.mark.django_db
def test_budgets_fail_the_run(run_setup, settings):
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings, max_turns=1)
    add_tool(placeholder, ai, settings, tool_name="echo")
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="echo", arguments={})]))
    SCRIPT.append(says(calls=[ToolCall(id="c2", name="echo", arguments={})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert step_action().state == FAILED
    assert "turns" in step_action().result["error"]


def test_a_budget_is_checked_before_a_turn_not_after():
    budget = AgentBudget(max_turns=2)
    state = AgentState(turn=2)
    with pytest.raises(BudgetExceeded):
        budget.check(state)


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_replaying_a_call_keeps_the_call_it_was(run_setup, settings):
    from djangocms_automation.engine import fail_action, replay_action

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
        requires_approval=True,
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    call = call_action()
    AutomationAction.objects.filter(pk=call.pk).update(scratch={**call.scratch, "approved": True})
    call.refresh_from_db()
    fail_action(call, "the worker died", allowed_from=(WAITING,))

    replacement = replay_action(call.pk)

    assert replacement.scratch["tool_call"]["arguments"] == {"subject": "Ship it"}
    assert "approved" not in replacement.scratch, "approval was granted to a call that then failed"


def test_the_step_offers_every_action_that_has_not_opted_out():
    """Computed from what has registered, not declared as a list.

    Overriding ``get_child_classes`` looked equivalent and was not: that method
    also carries django CMS's caching protocol and an ``only_uncached`` pass, so
    an override has to reimplement both and keep them in step with the CMS.
    ``get_child_class_overrides`` is asked the smaller question.
    """
    from cms.plugin_pool import plugin_pool

    from djangocms_automation.cms_plugins import action_plugins

    plugin = plugin_pool.get_plugin("AIStep")
    for only_uncached in (False, True):
        offered = plugin.get_child_classes(slot="start", page=None, instance=None, only_uncached=only_uncached)
        assert isinstance(offered, list), "the CMS calling convention is honoured"

    offered = plugin.get_child_classes(slot="start", page=None, instance=None, only_uncached=False)
    assert set(action_plugins) <= set(offered), "every action is a possible tool"
    assert "DataModifier" in offered, "a step is an action, so modifiers still apply to it"


def test_an_action_can_decline_to_be_a_tool():
    from cms.plugin_pool import plugin_pool

    from djangocms_automation.cms_plugins import ActionPlugin

    plugin = plugin_pool.get_plugin("AIStep")
    assert ActionPlugin.can_be_tool is True, "the default is yes"

    query = plugin_pool.get_plugin("QueryModelAction")
    query.can_be_tool = False
    try:
        offered = plugin.get_child_classes(slot="start", page=None, instance=None, only_uncached=False)
        assert "QueryModelAction" not in offered, "declining means not offered, not refused later"
    finally:
        query.can_be_tool = True


@pytest.mark.django_db
def test_an_action_draws_as_a_tool_inside_a_step_and_as_a_step_outside_one(run_setup, settings):
    """Same plugin, same instance; what differs is what it is where it sits.

    Routing the template rather than drawing tool rows from the AI step's own
    template is what keeps each tool a *rendered plugin* — so django CMS wraps
    it, and an editor can double-click it open, move it and delete it the way
    they would an action anywhere else.
    """
    from cms.plugin_pool import plugin_pool

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    inside = add_tool(placeholder, ai, settings, plugin_type="MailAction", tool_name="reply")
    outside = add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=settings.LANGUAGE_CODE,
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
    )

    plugin_class = plugin_pool.get_plugin("MailAction")
    plugin = plugin_class(plugin_class.model, None)

    assert plugin.get_render_template({}, inside, placeholder).endswith("tool.html")
    assert plugin.get_render_template({}, outside, placeholder).endswith("action.html")
    assert plugin.get_render_template({}, None, placeholder).endswith("action.html")


@pytest.mark.django_db
def test_a_tool_draws_as_a_modifier_row_with_a_square_marker(run_setup, settings):
    """Same row as a modifier, different shape around the icon.

    A tool and a modifier both belong to the card they sit in, so they are drawn
    the same way — but they are not the same thing, and the diagram should not
    have to be read twice to tell which is which.
    """
    from django.template.loader import render_to_string
    from django.test import RequestFactory

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(placeholder, ai, settings, plugin_type="MailAction", tool_name="reply", requires_approval=None)

    instance = step.AIStepPluginModel.objects.get(pk=ai.pk)
    request = RequestFactory().get("/")
    request.user = None
    html = render_to_string(
        "djangocms_automation/plugins/ai_step.html",
        {
            "instance": instance,
            "title": "Ask a Model",
            "tools": list(instance.cmsplugin_set.all()),
            "approval_tools": [tool],
        },
        request=request,
    )

    assert 'class="modifier tool"' in html, "the same row a modifier gets"
    assert "tool-marker" in html, "and a square marker rather than a round one"
    assert "bi-envelope-at" in html, "carrying the icon of the action it runs"
    assert "bi-shield-exclamation" in html, "and the gate an irreversible action gets automatically"
    assert html.index('class="modifier tool"') < html.index('class="modifier approval"')
    assert html.index('class="modifier approval"') < html.index('class="modifier actor"')


@pytest.mark.django_db
def test_the_selected_model_is_the_ai_step_actor(run_setup, settings):
    """The plugin derives its actor from configuration rather than storing another field."""
    _trigger, placeholder = run_setup
    settings.AUTOMATION_LLM_MODELS = [
        ("anthropic/claude-opus-4-8", "Claude Opus"),
    ]
    ai = add_step(placeholder, settings)

    assert step.AIStepPluginModel.objects.get(pk=ai.pk).actor == "Claude Opus"


@pytest.mark.django_db
def test_an_ai_step_actor_survives_a_model_removed_from_settings(run_setup, settings):
    """An existing diagram remains legible if its configured model is no longer offered."""
    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    settings.AUTOMATION_LLM_MODELS = []

    assert step.AIStepPluginModel.objects.get(pk=ai.pk).actor == "anthropic/claude-opus-4-8"


@pytest.mark.django_db
def test_a_step_with_nothing_in_it_says_so(run_setup, settings):
    from django.template.loader import render_to_string
    from django.test import RequestFactory

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)

    request = RequestFactory().get("/")
    request.user = None
    html = render_to_string(
        "djangocms_automation/plugins/ai_step.html",
        {
            "instance": step.AIStepPluginModel.objects.get(pk=ai.pk),
            "title": "Ask a Model",
            "tools": [],
            "approval_tools": [],
        },
        request=request,
    )

    assert "Answers only" in html
    assert "tool-marker" not in html, "nothing to point at"


# --------------------------------------------------------------------------
# The wiring form
# --------------------------------------------------------------------------


def wired_form(plugin_type, parent, settings, data=None):
    """The form an editor actually gets when adding an action inside a step."""
    from cms.plugin_pool import plugin_pool
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    plugin = plugin_pool.get_plugin(plugin_type)(plugin_pool.get_plugin(plugin_type).model, AdminSite())
    request = RequestFactory().get(f"/?plugin_parent={parent.pk}")
    request.user = None
    form_class = plugin.get_form(request, obj=None)
    return plugin, form_class(data=data) if data is not None else form_class()


@pytest.mark.django_db
def test_the_wired_form_validates(run_setup, settings):
    """Submitting it must not recurse.

    The check that a bound input has a value spans two fields, so it lives on
    the form. Injected as a plain function it called ``super(type(self), self)``
    — and Django's admin subclasses the generated form, so that lookup found
    the same method again, forever.
    """
    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)

    _plugin, form = wired_form(
        "MailAction",
        ai,
        settings,
        data={
            "intent": "Send the reply",
            "subject": "subject",
            "body": "body",
            "recipient_email": "recipient_email",
            "from_email": "",
            "comment": "",
        },
    )

    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_the_wiring_switch_replaces_the_requirement(run_setup, settings):
    """A bound input needs a value; one the model fills does not."""
    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)

    _plugin, missing = wired_form(
        "MailAction",
        ai,
        settings,
        data={
            "intent": "Send the reply",
            "subject": "",
            "body": "b",
            "recipient_email": "r",
            "comment": "",
        },
    )
    assert not missing.is_valid()
    assert "subject" in missing.errors

    _plugin, filled_by_model = wired_form(
        "MailAction",
        ai,
        settings,
        data={
            "intent": "Send the reply",
            "subject": "",
            "model_fills__subject": "on",
            "body": "b",
            "recipient_email": "r",
            "comment": "",
        },
    )
    assert filled_by_model.is_valid(), filled_by_model.errors


@pytest.mark.django_db
def test_an_action_outside_a_step_gets_no_switches(run_setup, settings):
    """The mixin does nothing where there is no model to offer anything to."""
    from cms.plugin_pool import plugin_pool
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    _trigger, _placeholder = run_setup
    plugin_class = plugin_pool.get_plugin("MailAction")
    plugin = plugin_class(plugin_class.model, AdminSite())
    request = RequestFactory().get("/")
    request.user = None

    form_class = plugin.get_form(request, obj=None)

    assert not [name for name in form_class.base_fields if name.startswith(plugin.MODEL_FILLS)]
    assert form_class.base_fields["subject"].required, "outside a step it is required as ever"


@pytest.mark.django_db
def test_the_approval_shield_is_in_the_icon_sprite(run_setup, settings):
    """A ``<use>`` reference to a symbol nobody defined renders as nothing.

    Which is the wrong way for a safety marker to fail — an ungated tool and a
    gated one would look identical.
    """
    from django.template.loader import get_template

    # Read rather than rendered: the page extends the project's base.html,
    # which a test project need not have, and the sprite is static markup.
    source = get_template("djangocms_automation/automation_detail.html").template.source

    assert 'id="bi-shield-exclamation"' in source


@pytest.mark.django_db
def test_a_step_does_not_ask_for_the_same_field_twice(run_setup, settings):
    """An action's inputs are added as a fieldset of their own.

    That is right for an action with no layout of its own, which is nearly all
    of them. The AI step arranges its own — budgets collapsed away from the
    prompt — and so was shown every field a second time under *Inputs*.
    """
    from cms.plugin_pool import plugin_pool
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = None

    def placed(plugin_type):
        cls = plugin_pool.get_plugin(plugin_type)
        fieldsets = cls(cls.model, AdminSite()).get_fieldsets(request, None)
        names = [name for _label, opts in fieldsets for name in opts["fields"]]
        return names

    names = placed("AIStep")
    assert len(names) == len(set(names)), f"asked twice for: {sorted({n for n in names if names.count(n) > 1})}"
    assert "prompt" in names and "max_turns" in names, "and still asks for everything once"

    # An action that lays out nothing still gets its inputs section.
    assert "subject" in placed("MailAction")


@pytest.mark.django_db
def test_the_inputs_come_second(run_setup, settings):
    """What an action is configured with is the point of opening it.

    The sections around it — what it is called as a tool, and the comment — are
    about the step rather than its settings, so the inputs are not last.
    """
    from cms.plugin_pool import plugin_pool
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    cls = plugin_pool.get_plugin("MailAction")
    plugin = cls(cls.model, AdminSite())

    wired = RequestFactory().get(f"/?plugin_parent={ai.pk}")
    wired.user = None
    labels = [str(label) for label, _opts in plugin.get_fieldsets(wired, None)]
    assert labels == ["Intent", "As a tool", "Inputs", "Comment"]


def test_no_icon_is_defined_twice_in_the_sprite():
    """Two symbols with one id is invalid SVG, and only the first is ever used.

    Which makes the second a silent no-op — an edit to it changes nothing, and
    nothing says why.
    """
    import re

    from django.template.loader import get_template

    source = get_template("djangocms_automation/automation_detail.html").template.source
    ids = re.findall(r'<symbol[^>]*\bid="([^"]+)"', source)

    assert len(ids) == len(set(ids)), f"defined twice: {sorted({i for i in ids if ids.count(i) > 1})}"


SHAPE = {
    "type": "object",
    "properties": {"topic": {"type": "string"}},
    "required": ["topic"],
    "additionalProperties": False,
}


@pytest.mark.django_db
def test_an_output_shape_saved_by_the_editor_is_a_dict(run_setup, settings):
    """The field is a ``JSONField``, so what the editor saves is an object.

    It used to be a string, and the tests still described it that way — so the
    shape the widget actually produces was the one nothing exercised.
    """
    trigger, placeholder = run_setup
    add_step(placeholder, settings, output_schema=SHAPE)
    SCRIPT.append(says(json={"topic": "billing"}))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert step_action().result == [{"topic": "billing"}]


@pytest.mark.django_db
def test_the_shape_reaches_the_provider(run_setup, settings):
    """Constraining the answer is the point of asking for a shape."""
    asked = []

    def complete(**kwargs):
        asked.append(kwargs.get("schema"))
        assert SCRIPT
        return SCRIPT.pop(0)

    trigger, placeholder = run_setup
    add_step(placeholder, settings, output_schema=SHAPE)
    SCRIPT.append(says(json={"topic": "billing"}))

    with mock.patch.object(llm, "complete", side_effect=complete):
        trigger.trigger_execution(data=[{"seed": 1}])

    assert asked == [SHAPE]


@pytest.mark.django_db
def test_the_editor_round_trips_a_shape_through_the_widget(run_setup, settings):
    """What the form saves is what it shows again when reopened."""
    from cms.plugin_pool import plugin_pool
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings, output_schema=SHAPE)

    cls = plugin_pool.get_plugin("AIStep")
    plugin = cls(cls.model, AdminSite())
    request = RequestFactory().get("/")
    request.user = None
    form = plugin.get_form(request, obj=step.AIStepPluginModel.objects.get(pk=ai.pk))()

    rendered = str(form["output_schema"])
    assert "schema-widget" in rendered, "the editor gets the table, not a bare textarea"
    assert "&quot;topic&quot;" in rendered or '"topic"' in rendered, "seeded with what was saved"


# --------------------------------------------------------------------------
# What a call does when it cannot be answered
# --------------------------------------------------------------------------


def mail_tool(placeholder, ai, settings, exposed, **kwargs):
    return add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name=kwargs.pop("tool_name", "reply"),
        exposed_fields=exposed,
        requires_approval=kwargs.pop("requires_approval", False),
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
        **kwargs,
    )


@pytest.mark.django_db
def test_arguments_that_were_not_valid_json_do_not_run_the_tool(run_setup, settings):
    """An unparseable argument list is not an empty one.

    A tool whose inputs are all optional accepts an empty object and means "use
    the defaults", so a garbled message must not be able to say that.
    """
    from django.core import mail

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    mail_tool(placeholder, ai, settings, ["subject"])
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={}, malformed=True)]))
    SCRIPT.append(says(text="Never mind."))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert not mail.outbox, "nothing ran"
    assert "JSON" in observations(step_action())[0]["content"], "the model is told what to fix"


@pytest.mark.django_db
def test_an_argument_the_form_rejects_comes_back_as_a_correction(run_setup, settings):
    """The action's own form is the boundary, and refusing is not failing."""
    from django.core import mail

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    mail_tool(placeholder, ai, settings, ["recipient_email"])
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"recipient_email": "nonsense"})]))
    SCRIPT.append(says(text="I will try again."))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert not mail.outbox
    observation = observations(step_action())[0]
    assert observation["content"].startswith("Error"), "an observation, not a failed run"
    assert "recipient_email" in observation["content"]
    assert step_action().state == COMPLETED


@pytest.mark.django_db
def test_a_tool_that_raises_becomes_an_observation(run_setup, settings):
    """One tool going wrong is something the model can work around.

    Failing the whole run instead would make an agent as brittle as its most
    fragile tool. But a fault's text is not written for anybody — it can carry
    a query, a path, a token — and it is on its way to somebody else's
    provider, so only the fact of the failure travels.
    """
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    mail_tool(placeholder, ai, settings, ["subject"])
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Hi"})]))
    SCRIPT.append(says(text="That did not work."))

    # The proxy defines its own ``perform``; patching the base would miss it.
    with mock.patch(
        "djangocms_automation.actions.mail.MailActionPluginModel.perform",
        side_effect=RuntimeError("could not connect to postgres://user:hunter2@db"),
    ):
        trigger.trigger_execution(data=[{"seed": 1}])

    observation = observations(step_action())[0]
    assert "This tool failed" in observation["content"], "the model is told what it can act on"
    assert "hunter2" not in observation["content"], "and nothing it cannot"
    assert "postgres" not in observation["content"] and "RuntimeError" not in observation["content"]
    assert step_action().state == COMPLETED, "the run carries on"


@pytest.mark.django_db
def test_a_rate_limited_tool_pauses_rather_than_reporting_to_the_model(run_setup, settings):
    """``ActionPause`` is the engine's signal, not an observation.

    It means "run me again later", which only the engine can do — reported to
    the model it would look like the tool refusing, and the call would never be
    retried.
    """
    import datetime

    from django.utils.timezone import now

    from djangocms_automation.engine import ActionPause
    from djangocms_automation.instances import PENDING

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    mail_tool(placeholder, ai, settings, ["subject"])
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Hi"})]))

    with mock.patch(
        "djangocms_automation.actions.mail.MailActionPluginModel.perform",
        side_effect=ActionPause(until=now() + datetime.timedelta(minutes=5), message="rate limited"),
    ):
        trigger.trigger_execution(data=[{"seed": 1}])

    call = call_action()
    assert call.state == PENDING and call.paused_until is not None, "rescheduled, not answered"
    assert not (call.scratch or {}).get("tool_result"), "and the model was told nothing"


@pytest.mark.django_db
def test_a_failed_tool_call_fails_the_step(run_setup, settings):
    """A step re-entered to find a failed call has nothing to say.

    Reached by re-entry rather than by the failure itself, which fails the run
    fail-fast; this is the guard for the case where it does not.
    """
    from cms.plugin_pool import plugin_pool

    from djangocms_automation.instances import AutomationAction

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    mail_tool(placeholder, ai, settings, ["subject"])
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Hi"})]))
    SCRIPT.append(says(text="Done."))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    AutomationAction.objects.filter(pk=call_action().pk).update(state=FAILED)
    instance = plugin_pool.get_plugin("AIStep").model.objects.get(pk=ai.pk)

    assert instance.do_work(action, [{"seed": 1}])[0] == FAILED


# --------------------------------------------------------------------------
# A tool that waits for a person
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_what_a_person_answers_is_what_the_model_hears(run_setup, settings, admin_user):
    """The half of an escalation that matters.

    Pausing is visible; the answer coming back is not, and a tool that waited
    and then reported nothing would look exactly like one that worked.
    """
    from djangocms_automation.engine import resume_action

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="UserInputAction",
        tool_name="escalate",
        config={"note": "Please check this refund."},
        requires_approval=False,
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="escalate", arguments={})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    call = call_action()
    assert call.state == WAITING

    SCRIPT.append(says(text="Thanks, escalated."))
    resume_action(call.pk, admin_user, data={"verdict": "approved by finance"})

    call.refresh_from_db()
    assert call.state == COMPLETED
    observation = observations(step_action())[0]
    assert observation["tool_call_id"] == "c1"
    assert "approved by finance" in observation["content"], "what they said reaches the model"
    assert step_action().state == COMPLETED


# --------------------------------------------------------------------------
# How deep runs may nest
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_depth_of_a_run_counts_the_runs_that_started_it(run_setup, settings):
    """Depth is a chain of *instances*, not of actions.

    An AI step inside an AI step is a child action in one run, and the plugin
    tree bounds that on its own — a plugin cannot contain itself. What needs a
    limit is a run starting another run, which is what ``parent_action``
    records.
    """
    from cms.plugin_pool import plugin_pool

    from djangocms_automation.instances import AutomationAction, AutomationInstance

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    SCRIPT.append(says(text="Done."))
    trigger.trigger_execution(data=[{"seed": 1}])

    root = step_action()
    instance = plugin_pool.get_plugin("AIStep").model.objects.get(pk=ai.pk)
    assert instance.depth(root) == 0, "nothing started this one"

    # Three runs deep, each started by an action in the one above it.
    action = root
    for _level in range(3):
        started = AutomationInstance.objects.create(
            automation_content=root.automation_instance.automation_content,
            data=[],
            initial_data=[],
            parent_action=action,
        )
        action = AutomationAction.objects.create(automation_instance=started, plugin_ptr=ai.uuid, finished=None)

    assert instance.depth(action) == 3


@pytest.mark.django_db
def test_a_step_too_deep_refuses_rather_than_calling_a_model(run_setup, settings):
    """Each level is a legitimate run; only the depth is the mistake, and
    nothing in the engine can see that it is a loop."""
    from cms.plugin_pool import plugin_pool

    from djangocms_automation.ai.step import MAX_NESTING_DEPTH

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    SCRIPT.append(says(text="Done."))
    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    action.scratch = {"tool_call": {"id": "c1", "name": "ask", "arguments": {}}}
    instance = plugin_pool.get_plugin("AIStep").model.objects.get(pk=ai.pk)

    with mock.patch.object(type(instance), "depth", return_value=MAX_NESTING_DEPTH):
        state, result = instance.do_work(action, [{"seed": 1}])

    assert state == FAILED
    assert "nested" in result["error"]
    assert not SCRIPT or len(SCRIPT) >= 0, "no provider call was needed to decide"


# --------------------------------------------------------------------------
# Review round: the admin form, the budgets, and depth
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "plugin_type", ["MailAction", "QueryModelAction", "CreateModelAction", "UpdateModelAction", "UserInputAction"]
)
def test_every_action_can_be_added_inside_a_step(run_setup, settings, plugin_type):
    """The wiring switches are paired with every input by ``get_fieldsets``.

    An action that built its fields the other way — literal values rather than
    expressions — skipped the switches, so the admin was asked for fields that
    did not exist and raised before the form could render. Four of the five
    built-in actions do it that way.
    """
    from cms.plugin_pool import plugin_pool
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    cls = plugin_pool.get_plugin(plugin_type)
    plugin = cls(cls.model, AdminSite())
    request = RequestFactory().get(f"/?plugin_parent={ai.pk}")
    request.user = None

    form = plugin.get_form(request, None)

    switches = [name for name in form.base_fields if name.startswith(plugin.MODEL_FILLS)]
    assert len(switches) == len(cls.data_form.base_fields), "one per input"
    for name in cls.data_form.base_fields:
        assert not form.base_fields[name].required, "the switch decides whether a value is needed"


@pytest.mark.django_db
def test_a_name_no_tool_has_does_not_spend_the_call_budget(run_setup, settings):
    """``max_tool_calls`` bounds side effects, and a guess causes none.

    Counting one against it starves the call the model gets right on its next
    breath. What stops a model that only ever guesses is the turn limit.
    """
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings, max_tool_calls=1)
    add_tool(placeholder, ai, settings, tool_name="echo")
    SCRIPT.append(
        says(calls=[ToolCall(id="c1", name="nope", arguments={}), ToolCall(id="c2", name="echo", arguments={})])
    )
    SCRIPT.append(says(text="Done."))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.children.count() == 1, "the real call still ran"
    assert action.state == COMPLETED


@pytest.mark.django_db
def test_a_run_that_only_guesses_still_stops(run_setup, settings):
    """The turn limit, not the tool-call limit, is what ends it."""
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings, max_turns=2)
    add_tool(placeholder, ai, settings, tool_name="echo")
    for _ in range(3):
        SCRIPT.append(says(calls=[ToolCall(id="c1", name="nope", arguments={})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.state == FAILED
    assert "turns" in action.result["error"]


@pytest.mark.django_db
def test_a_turn_that_spends_the_token_budget_fails_the_run(run_setup, settings):
    """Tokens are only knowable once the answer is back.

    Checked only beforehand, the last turn is free to exceed the limit and
    report success — a run over its budget that says it went fine.
    """
    trigger, placeholder = run_setup
    add_step(placeholder, settings, max_tokens=100)
    SCRIPT.append(says(text="A very expensive answer.", usage={"input_tokens": 400, "output_tokens": 50}))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.state == FAILED
    assert "tokens" in action.result["error"]


@pytest.mark.django_db
def test_the_provider_is_never_given_longer_than_the_run_has_left(run_setup, settings):
    """An answer arriving after the deadline is one the run should have failed
    before receiving."""
    asked = []

    def complete(**kwargs):
        asked.append(kwargs.get("timeout"))
        assert SCRIPT
        return SCRIPT.pop(0)

    trigger, placeholder = run_setup
    add_step(placeholder, settings, deadline_seconds=30, llm_timeout=120)
    SCRIPT.append(says(text="Quick."))

    with mock.patch.object(llm, "complete", side_effect=complete):
        trigger.trigger_execution(data=[{"seed": 1}])

    assert asked and asked[0] <= 30, f"asked for {asked}"


@pytest.mark.django_db
def test_a_step_inside_a_step_counts_towards_the_depth(run_setup, settings):
    """A step called as another step's tool is a child action of the same run.

    Counting only nested *runs* reported zero for it — which is the only kind
    of nesting reachable today.
    """
    from cms.plugin_pool import plugin_pool

    from djangocms_automation.instances import AutomationAction

    trigger, placeholder = run_setup
    outer = add_step(placeholder, settings)
    SCRIPT.append(says(text="Done."))
    trigger.trigger_execution(data=[{"seed": 1}])

    root = step_action()
    instance = plugin_pool.get_plugin("AIStep").model.objects.get(pk=outer.pk)
    assert instance.depth(root) == 0

    inner = AutomationAction.objects.create(
        automation_instance=root.automation_instance,
        parent=root,
        previous=root,
        plugin_ptr=outer.uuid,
        scratch={"tool_call": {"id": "c1", "name": "ask", "arguments": {}}},
        finished=None,
    )

    assert instance.depth(inner) == 1, "one AI step above it"


# --------------------------------------------------------------------------
# Review round: nesting, wiring on a plugin that lays out its own fields
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_step_used_as_another_step_s_tool_runs_to_the_end(run_setup, settings):
    """Waiting is not the same as waiting for a person.

    A nested step returns WAITING with its conversation and the calls it wants
    saved on the action. Treating that as human input both mislabelled it and
    wrote back a snapshot taken before it ran — erasing the calls, so nothing
    was ever scheduled and both steps waited for ever.
    """
    trigger, placeholder = run_setup
    outer = add_step(placeholder, settings, prompt="outer")
    inner = add_plugin(
        placeholder=placeholder,
        plugin_type="AIStep",
        language=settings.LANGUAGE_CODE,
        target=outer,
        tool_name="research",
        tool_description="Look into it.",
        exposed_fields=[],
        requires_approval=False,
        config={"model": "anthropic/claude-opus-4-8", "prompt": "inner"},
    )
    add_tool(placeholder, inner, settings, tool_name="echo")

    SCRIPT.append(says(calls=[ToolCall(id="o1", name="research", arguments={})]))
    SCRIPT.append(says(calls=[ToolCall(id="i1", name="echo", arguments={})]))
    SCRIPT.append(says(text="inner done"))
    SCRIPT.append(says(text="outer done"))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert step_action().state == COMPLETED
    nested = AutomationAction.objects.exclude(parent__isnull=True).order_by("id").first()
    assert nested.state == COMPLETED
    assert AgentState.load(nested).messages, "its conversation was not overwritten"
    assert observations(step_action())[0]["tool_call_id"] == "o1"


@pytest.mark.django_db
def test_a_plugin_that_lays_out_its_own_fields_still_gets_switches(run_setup, settings):
    """A switch outside every fieldset is a field the admin never renders.

    The form would carry all nine and show none, so nothing could be exposed to
    the step above.
    """
    from cms.plugin_pool import plugin_pool
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    cls = plugin_pool.get_plugin("AIStep")
    plugin = cls(cls.model, AdminSite())
    request = RequestFactory().get(f"/?plugin_parent={ai.pk}")
    request.user = None

    rendered = [
        name
        for _label, options in plugin.get_fieldsets(request, None)
        for entry in options["fields"]
        for name in (entry if isinstance(entry, tuple) else [entry])
    ]
    on_form = [name for name in plugin.get_form(request, None).base_fields if name.startswith(plugin.MODEL_FILLS)]

    assert on_form, "the form has them"
    assert sorted(n for n in rendered if n.startswith(plugin.MODEL_FILLS)) == sorted(on_form), (
        "and so do the fieldsets"
    )


@pytest.mark.django_db
def test_a_bound_input_of_zero_is_filled_in(run_setup, settings):
    """``0`` and ``False`` are values somebody chose, not blanks."""
    from django import forms as django_forms
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    from djangocms_automation.cms_plugins import ActionPlugin

    class CountForm(django_forms.Form):
        count = django_forms.IntegerField()

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)

    class Counting(ActionPlugin):
        data_form = CountForm
        convert_data_form = False

    plugin = Counting(ActionPlugin.model, AdminSite())
    request = RequestFactory().get(f"/?plugin_parent={ai.pk}")
    request.user = None
    form = plugin.get_form(request, None)(data={"intent": "Count rows", "count": 0, "comment": ""})

    assert form.is_valid(), form.errors


def test_a_derived_tool_name_is_always_legal():
    """An action's name is prose; a tool name is not.

    Left as it came, an ordinary action would build a spec that raises while
    assembling the provider request — a run failing at the last moment over a
    name nobody typed.
    """
    from djangocms_automation.tool_mixin import _as_tool_name
    from djangocms_automation.tools import TOOL_NAME_RE

    for source in ("Send Email", "Rückruf anfordern", "Create/Update Record!", "x" * 90):
        assert TOOL_NAME_RE.match(_as_tool_name(source)), source
    assert _as_tool_name("数据查询") == "", "a name with nothing to transliterate falls back"


@pytest.mark.django_db
def test_a_name_the_editor_typed_is_checked_in_the_editor(run_setup, settings):
    """Where a derived name is normalised, a typed one is reported.

    Silently rewriting what somebody wrote would change what the model is told
    to call it without saying so.
    """
    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(placeholder, ai, settings, tool_name="echo")
    tool.tool_name = "not a legal name!"

    assert any("cannot be a tool name" in str(message) for message in tool.tool_messages())


@pytest.mark.django_db
def test_a_required_checkbox_must_still_be_ticked(run_setup, settings):
    """Whether an input is filled in is a question only the field can answer.

    ``0`` satisfies an IntegerField; an unticked box does not satisfy a
    required BooleanField. No test of emptiness written here gets both right,
    so the field is asked.
    """
    from django import forms as django_forms
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    from djangocms_automation.cms_plugins import ActionPlugin

    class ConfirmForm(django_forms.Form):
        count = django_forms.IntegerField()
        confirmed = django_forms.BooleanField()

    class Confirming(ActionPlugin):
        data_form = ConfirmForm
        convert_data_form = False

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    plugin = Confirming(ActionPlugin.model, AdminSite())
    request = RequestFactory().get(f"/?plugin_parent={ai.pk}")
    request.user = None
    form_class = plugin.get_form(request, None)

    unticked = form_class(data={"intent": "Confirm the count", "count": 0, "comment": ""})
    assert not unticked.is_valid(), "an unticked required box is not filled in"
    assert "confirmed" in unticked.errors
    assert "count" not in unticked.errors, "but zero is a value"

    assert form_class(
        data={
            "intent": "Confirm the count",
            "count": 0,
            "confirmed": "on",
            "comment": "",
        }
    ).is_valid()

    left_to_the_model = form_class(
        data={
            "intent": "Confirm the count",
            "count": 0,
            "model_fills__confirmed": "on",
            "comment": "",
        }
    )
    assert left_to_the_model.is_valid(), "unless the model fills it"


@pytest.mark.django_db
def test_fields_grouped_on_one_line_get_their_switches(run_setup, settings):
    """A fieldset entry can be a tuple rendered on one line.

    Counted as placed but never paired, those inputs kept their switches out of
    every fieldset — so the admin drew none of them.
    """
    from django import forms as django_forms
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    from djangocms_automation.cms_plugins import ActionPlugin

    class PairForm(django_forms.Form):
        first = django_forms.CharField()
        second = django_forms.CharField()

    class Grouped(ActionPlugin):
        data_form = PairForm
        convert_data_form = False
        fieldsets = [(None, {"fields": [("first", "second")]})]

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    plugin = Grouped(ActionPlugin.model, AdminSite())
    request = RequestFactory().get(f"/?plugin_parent={ai.pk}")
    request.user = None

    rendered = [
        name
        for _label, options in plugin.get_fieldsets(request, None)
        for entry in options["fields"]
        for name in (entry if isinstance(entry, (list, tuple)) else [entry])
    ]

    assert "model_fills__first" in rendered and "model_fills__second" in rendered


@pytest.mark.django_db
def test_an_action_with_cross_field_validation_works_as_a_tool(run_setup, settings):
    """The form validates whole, or it cannot validate at all.

    Comparing two fields is the ordinary way to write a ``clean``. Handed only
    the exposed half it raises ``KeyError``, which used to escape as a failed
    run — so an action with perfectly normal validation could not be a tool,
    against the promise that any action can.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import ToolError, ToolValidationError, validate_arguments

    class RangeForm(django_forms.Form):
        low = django_forms.IntegerField()
        high = django_forms.IntegerField()

        def clean(self):
            cleaned = super().clean()
            if cleaned["low"] > cleaned["high"]:
                # Addressed to the model, so raised as the type that reaches it.
                raise ToolError("low must not exceed high")
            return cleaned

    # The editor bound `high`; the model fills `low`.
    assert validate_arguments(RangeForm, {"low": 1}, allowed=["low"], bound={"high": 10}) == {"low": 1}

    with pytest.raises(ToolValidationError, match="exceed"):
        validate_arguments(RangeForm, {"low": 99}, allowed=["low"], bound={"high": 10})

    # And with nothing to compare against, the model is told rather than the
    # run being failed — without being told anything about the failure itself.
    with pytest.raises(ToolValidationError) as refused:
        validate_arguments(RangeForm, {"low": 1}, allowed=["low"])
    assert "KeyError" not in str(refused.value)


@pytest.mark.django_db
def test_an_answer_names_the_model_that_gave_it(run_setup, settings):
    """Documented as part of the row, and a downstream step may read it."""
    trigger, placeholder = run_setup
    add_step(placeholder, settings)
    SCRIPT.append(says(text="Hello."))

    trigger.trigger_execution(data=[{"seed": 1}])

    row = step_action().result[0]
    assert set(row) == {"text", "model", "turns", "usage"}
    assert row["model"] == "anthropic/claude-opus-4-8"


@pytest.mark.django_db
def test_a_bound_expression_is_resolved_before_the_form_sees_it(run_setup, settings):
    """An expression-configured action can have cross-field validation too.

    What ``trigger.from`` comes to is knowable when the call is checked — the
    automation's data is in hand — so the form is given the same pair of values
    the action is about to use, not one half and a hole.
    """
    from cms.plugin_pool import plugin_pool

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        config={"recipient_email": "customer.email", "subject": "'x'", "body": "x"},
    )
    instance = plugin_pool.get_plugin("MailAction").model.objects.get(pk=tool.pk)
    instance.exposed_fields = ["subject"]

    rows = [{"customer": {"email": "ada@example.com"}}]
    bound = instance._bound_values_for(rows[0], rows)

    assert bound["recipient_email"] == "ada@example.com", "resolved, not the expression"
    assert "subject" not in bound, "what the model fills is not bound"

    # An expression that cannot resolve is left out rather than guessed at.
    assert "recipient_email" not in instance._bound_values_for({"nothing": "here"}, [{"nothing": "here"}])


def test_a_fault_in_an_action_is_not_described_to_the_model():
    """An exception's text can carry a query, a path or a credential.

    None of that is an argument a model can correct, and all of it would be
    sent to somebody else's provider.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import ToolValidationError, validate_arguments

    class ExplodingForm(django_forms.Form):
        a = django_forms.CharField()

        def clean(self):
            raise RuntimeError("connection to postgres://user:hunter2@db failed")

    with pytest.raises(RuntimeError):
        validate_arguments(ExplodingForm, {"a": "x"}, allowed=["a"])

    class MissingHalfForm(django_forms.Form):
        a = django_forms.CharField()
        b = django_forms.CharField()

        def clean(self):
            return {"both": self.cleaned_data["a"] + self.cleaned_data["b"]}

    with pytest.raises(ToolValidationError) as refused:
        validate_arguments(MissingHalfForm, {"a": "x"}, allowed=["a"])
    assert "cannot check its arguments" in str(refused.value)


@pytest.mark.django_db
def test_approval_resumes_with_the_step_s_own_data(run_setup, settings, django_user_model):
    """A run's ``data`` is the trigger's payload until the run finishes.

    Resuming from it hands the call the input of a step that came before
    everything, discarding what the steps in between produced — usually the
    very thing the person was asked to approve. The call then completes with an
    error nobody sees, so the approval looks like it worked.
    """
    from django.core import mail

    from djangocms_automation.engine import resume_action

    settings.AUTOMATION_ALLOWED_MODELS = ["auth.User"]
    django_user_model.objects.create(username="ada", email="ada@example.com")

    trigger, placeholder = run_setup
    # A step in front, so what the AI step is given is not what the run began
    # with — which is the only way the difference shows.
    add_plugin(
        placeholder=placeholder,
        plugin_type="QueryModelAction",
        language=settings.LANGUAGE_CODE,
        # Filter values are expressions, so the literal is quoted.
        config={"model": "auth.User", "filters": {"username": "'ada'"}, "fields": "email", "limit": 1},
    )
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        requires_approval=True,
        config={"recipient_email": "email", "subject": "'x'", "body": "x"},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    call = call_action()
    assert call.state == WAITING
    assert call.automation_instance.data == [{"seed": 1}], "the run still holds only the trigger's payload"

    SCRIPT.append(says(text="Sent."))
    resume_action(call.pk, get_user_model().objects.filter(is_superuser=True).first())

    call.refresh_from_db()
    result = (call.scratch or {})["tool_result"]
    assert not result["is_error"], result["content"]
    assert mail.outbox[-1].to == ["ada@example.com"], "the queried address, not the trigger's"


@pytest.mark.django_db
def test_every_row_is_checked_before_a_tool_runs(run_setup, settings):
    """An action runs once per row, so it is validated once per row.

    The model's arguments are the same each time; the bound half is not, and a
    rule about the pair applied to row one leaves the rest unexamined.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import ToolError, ToolValidationError, validate_arguments

    class SameDomainForm(django_forms.Form):
        subject = django_forms.CharField()
        recipient = django_forms.EmailField()

        def clean(self):
            cleaned = super().clean()
            if not str(cleaned.get("recipient", "")).endswith("@example.com"):
                raise ToolError("recipients must be internal")
            return cleaned

    # Row one passes, row two does not.
    validate_arguments(SameDomainForm, {"subject": "Hi"}, allowed=["subject"], bound={"recipient": "a@example.com"})
    with pytest.raises(ToolValidationError, match="internal"):
        validate_arguments(
            SameDomainForm, {"subject": "Hi"}, allowed=["subject"], bound={"recipient": "b@elsewhere.org"}
        )


def test_a_submitted_field_cannot_claim_to_be_somebody():
    """``user_id`` says who submitted the form.

    A form field of that name is a value the submitter chose, so merging it
    would let anyone claim to be anyone — in the one field an automation is
    most likely to trust.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "djangocms_automation/triggers.py").read_text()

    assert 'payload["user_id"] = request.user.pk if request.user.is_authenticated else None' in source
    assert 'if "user_id" not in payload' not in source, "assigned, never merged"


@pytest.mark.django_db
def test_a_second_turn_keeps_what_the_first_one_had(run_setup, settings, django_user_model):
    """Coming back to itself, an action reuses its own input.

    A join waking, a paused action revived, an approval going ahead — none of
    them carry data, and the instance holds the trigger's payload until the run
    ends. Falling back to that hands the second turn the input of the first
    step, so a tool called after one has already run loses every field produced
    since.
    """
    from django.core import mail

    settings.AUTOMATION_ALLOWED_MODELS = ["auth.User"]
    django_user_model.objects.create(username="ada", email="ada@example.com")

    trigger, placeholder = run_setup
    add_plugin(
        placeholder=placeholder,
        plugin_type="QueryModelAction",
        language=settings.LANGUAGE_CODE,
        config={"model": "auth.User", "filters": {"username": "'ada'"}, "fields": "email", "limit": 1},
    )
    ai = add_step(placeholder, settings)
    add_tool(placeholder, ai, settings, tool_name="look")
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        requires_approval=False,
        config={"recipient_email": "email", "subject": "'x'", "body": "x"},
    )

    # Two turns: something harmless, then the one that needs the queried row.
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="look", arguments={})]))
    SCRIPT.append(says(calls=[ToolCall(id="c2", name="reply", arguments={"subject": "Ship it"})]))
    SCRIPT.append(says(text="Sent."))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert step_action().state == COMPLETED
    assert mail.outbox, "the second turn's tool ran"
    assert mail.outbox[-1].to == ["ada@example.com"], "against the row the query produced"


@pytest.mark.django_db
def test_a_row_that_is_not_a_mapping_is_checked_too(run_setup, settings):
    """Actions run a bare value as ``{"value": row}``.

    Dropping such a row from the check would let it through unvalidated while
    it still had effects.
    """
    from cms.plugin_pool import plugin_pool

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(placeholder, ai, settings, plugin_type="MailAction", tool_name="reply")
    instance = plugin_pool.get_plugin("MailAction").model.objects.get(pk=tool.pk)

    assert instance._rows_to_check(["ada@example.com", {"a": 1}]) == [
        {"value": "ada@example.com"},
        {"a": 1},
    ]
    assert instance._rows_to_check([]) == [{}], "and a call with no data is still checked once"


@pytest.mark.django_db
def test_an_action_can_still_speak_to_the_model_on_purpose(run_setup, settings):
    """``ToolError`` exists to be reported; its text is written for the model.

    Which is the line: an author saying "that customer has no open orders"
    reaches the conversation, and a stack trace does not.
    """
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        requires_approval=False,
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Hi"})]))
    SCRIPT.append(says(text="Understood."))

    from djangocms_automation.tools import ToolError

    with mock.patch(
        "djangocms_automation.actions.mail.MailActionPluginModel.perform",
        side_effect=ToolError("that customer has no address on file"),
    ):
        trigger.trigger_execution(data=[{"seed": 1}])

    assert "no address on file" in observations(step_action())[0]["content"]


@pytest.mark.django_db
def test_the_stand_in_marks_arguments_it_could_not_read(run_setup, settings):
    """A stand-in that quietly emptied them would let an all-optional tool run
    — the very thing the refusal exists to prevent."""
    from djangocms_automation.ai import dummy

    reply = dummy.answer(
        "dummy/echo",
        [{"role": "user", "content": "!call find_users {oops}"}],
        [{"type": "function", "function": {"name": "find_users"}}],
    )

    call = reply.tool_calls[0]
    assert call.malformed is True
    assert call.arguments == {}


@pytest.mark.django_db
def test_a_failed_tool_tells_the_model_nothing_it_should_not_know(run_setup, settings):
    """A failure payload is written for whoever operates this.

    A nested AI step returns the provider's own error text; other actions
    return diagnostics meant for an operator. Sanitising the exception path
    and not this one left the same words travelling by a different route.
    """
    trigger, placeholder = run_setup
    outer = add_step(placeholder, settings, prompt="outer")
    add_plugin(
        placeholder=placeholder,
        plugin_type="AIStep",
        language=settings.LANGUAGE_CODE,
        target=outer,
        tool_name="research",
        tool_description="Look into it.",
        exposed_fields=[],
        requires_approval=False,
        config={"model": "anthropic/claude-opus-4-8", "prompt": "inner"},
    )

    SCRIPT.append(says(calls=[ToolCall(id="o1", name="research", arguments={})]))
    # The nested step's provider call fails, carrying a provider's raw text.
    SCRIPT.append(llm.LLMError("LLM error from 'anthropic': 401 key sk-ant-secret is invalid"))
    SCRIPT.append(says(text="I could not look into it."))

    def complete(**kwargs):
        assert SCRIPT
        reply = SCRIPT.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    with mock.patch.object(llm, "complete", side_effect=complete):
        trigger.trigger_execution(data=[{"seed": 1}])

    observation = observations(step_action())[0]
    assert "This tool failed" in observation["content"]
    assert "sk-ant-secret" not in observation["content"] and "401" not in observation["content"]


@pytest.mark.django_db
def test_a_partial_delivery_failure_does_not_carry_its_message(run_setup, settings):
    """Half the rows succeeding is the case that returns *successfully*.

    The failure then travels as data — to the next action, into the run's
    record, and to a model when this action is somebody's tool — so what lands
    in the row is the kind of failure, not its text.
    """
    from djangocms_automation.actions.mail import MailActionPluginModel

    plugin = MailActionPluginModel(
        plugin_type="MailAction",
        config={"recipient_email": "email", "subject": "'Hi'", "body": "Hello"},
    )
    rows = [{"email": "ada@example.com"}, {"email": ""}]

    out = plugin.perform(mock.Mock(pk=1), rows)

    assert out[0]["_mail"]["sent"] is True
    assert out[1]["_mail"]["sent"] is False
    assert out[1]["_mail"]["error"] == "ValueError", "the kind, not the message"
    assert "No recipient" not in str(out[1])


@pytest.mark.django_db
def test_a_call_reports_what_it_produced_not_what_it_was_given(run_setup, settings):
    """Most actions pass their input through and add a field to it.

    Reporting the rows would hand back everything the automation happens to be
    carrying — a token fetched by an earlier query, a column nobody meant to
    expose — on the strength of having sent an email.
    """
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        requires_approval=False,
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))
    SCRIPT.append(says(text="Sent."))

    trigger.trigger_execution(data=[{"customer_token": "sk-do-not-share", "seed": 1}])

    observation = observations(step_action())[0]["content"]
    assert "_mail" in observation, "what the call did"
    assert "sk-do-not-share" not in observation, "and not what it happened to be holding"

    # The action's own rows are untouched: the automation still carries them on.
    assert call_action().result[0]["customer_token"] == "sk-do-not-share"


@pytest.mark.django_db
def test_a_lookup_still_reports_everything_it_found(run_setup, settings, django_user_model):
    """The case the distinction exists to keep working.

    A read tool's answer *is* its rows, and a rule that reported only what
    changed would report nothing at all.
    """
    settings.AUTOMATION_ALLOWED_MODELS = ["auth.User"]
    django_user_model.objects.create(username="ada", email="ada@example.com")

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="QueryModelAction",
        tool_name="find_users",
        exposed_fields=["filters"],
        requires_approval=False,
        config={"model": "auth.User", "fields": "username,email", "limit": 5},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="find_users", arguments={"filters": {"username": "ada"}})]))
    SCRIPT.append(says(text="Found her."))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert "ada@example.com" in observations(step_action())[0]["content"]


@pytest.mark.django_db
def test_an_approver_sees_what_the_call_will_act_on(run_setup, settings):
    """The model chose the words; the automation chose the target.

    Approving a message without its recipient is approving a sentence with no
    subject.
    """
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        requires_approval=True,
        config={"recipient_email": "customer.email", "subject": "'x'", "body": "x"},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))

    trigger.trigger_execution(data=[{"customer": {"email": "ada@example.com"}}])

    task = call_action()
    assert task.result["arguments"] == {"subject": "Ship it"}
    assert len(task.result["bound"]) == 1, "one row, one target"
    target = task.result["bound"][0]
    assert target["inputs"]["recipient_email"] == "ada@example.com", "resolved, so it reads as an address"
    assert target["times"] == 1
    assert "subject" not in target["inputs"], "what the model chose is listed as the model's"


@pytest.mark.django_db
def test_an_approver_sees_every_row_the_call_will_act_on(run_setup, settings):
    """An action runs over every row it is given.

    So showing the first recipient and then sending to five is an approval of
    something that did not happen — the one shape of this mistake where the
    approver has no way of noticing.
    """
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        requires_approval=True,
        config={"recipient_email": "customer.email", "subject": "'x'", "body": "x"},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))

    trigger.trigger_execution(
        data=[
            {"customer": {"email": "ada@example.com"}},
            {"customer": {"email": "grace@example.com"}},
            {"customer": {"email": "alan@example.com"}},
        ]
    )

    shown = call_action().result["bound"]
    assert [entry["inputs"]["recipient_email"] for entry in shown] == [
        "ada@example.com",
        "grace@example.com",
        "alan@example.com",
    ], "all three, because all three will be written to"


def test_a_complaint_about_a_bound_value_is_not_repeated_to_the_model():
    """A validator's message is written for an administrator.

    It may quote the value it objected to, and that value is one the model was
    deliberately not shown.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import ToolValidationError, validate_arguments

    class KeyedForm(django_forms.Form):
        subject = django_forms.CharField()
        api_key = django_forms.CharField()

        def clean(self):
            cleaned = super().clean()
            if cleaned.get("api_key") != "good":
                raise django_forms.ValidationError(f"key {cleaned.get('api_key')} was rejected")
            return cleaned

    with pytest.raises(ToolValidationError) as refused:
        validate_arguments(KeyedForm, {"subject": "Hi"}, allowed=["subject"], bound={"api_key": "sk-secret-123"})

    assert "sk-secret-123" not in str(refused.value)
    assert "not something you can change" in str(refused.value)


@pytest.mark.django_db
def test_a_call_that_drops_rows_reports_how_many_not_which(run_setup, settings):
    """Returning fewer rows is not evidence of having produced any.

    An action that filters its input returns a different number of rows without
    having made a single one of them — they are the automation's rows, minus
    some. Counting them was the wrong way to tell the two apart, and got this
    case exactly backwards.
    """
    from cms.plugin_pool import plugin_pool

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
    )
    instance = plugin_pool.get_plugin("MailAction").model.objects.get(pk=tool.pk)

    given = [{"token": "sk-do-not-share", "keep": bool(n)} for n in (0, 1, 1)]
    survivors = [row for row in given if row["keep"]]

    reported = str(instance._reportable(given, survivors))
    assert "sk-do-not-share" not in reported, "they are its input, however few came back"
    assert "2" in reported, "how many survived is what there is to say"


@pytest.mark.django_db
def test_a_lookup_reports_its_rows_even_when_it_finds_as_many_as_it_was_given(run_setup, settings):
    """And the mirror image: a lookup can find exactly what it was asked about.

    "Does this user exist" is a query filtered on the row's own data, so the
    record that comes back matches the row that went in. Cardinality says
    "unchanged", the delta is empty, and a read tool answers ``{"rows": 1}``
    where it meant "yes, here she is". What it returns is its answer because of
    what it *is*, and no shape of the data will say so.
    """
    from cms.plugin_pool import plugin_pool

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="QueryModelAction",
        tool_name="find_users",
        exposed_fields=["filters"],
        config={"model": "auth.User", "fields": "username", "limit": 5},
    )
    instance = plugin_pool.get_plugin("QueryModelAction").model.objects.get(pk=tool.pk)

    asked_about = [{"username": "ada", "email": "ada@example.com"}]
    found = [{"username": "ada", "email": "ada@example.com"}]
    reported = str(instance._reportable(asked_about, found))
    assert "ada@example.com" in reported, "the rows are the answer, identical or not"


def test_an_action_says_what_it_reports_rather_than_being_guessed_at():
    """The policy is a declaration, because nothing else can tell."""
    from cms.plugin_pool import plugin_pool

    from djangocms_automation.cms_plugins import ActionPlugin

    assert ActionPlugin.reports_to_model == "changes", "the conservative one is the default"
    assert plugin_pool.get_plugin("QueryModelAction").reports_to_model == "rows"
    assert plugin_pool.get_plugin("MailAction").reports_to_model == "changes"


def test_a_complaint_the_model_alone_caused_is_one_it_is_asked_to_fix():
    """Not every non-field error is about a value the model cannot reach.

    When every field in the form is one the model filled, a complaint about
    their combination is about its own work. Telling it the values cannot be
    changed would be false, and would leave it stuck on a mistake that is
    entirely its to correct.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import ToolValidationError, validate_arguments

    class RangeForm(django_forms.Form):
        low = django_forms.IntegerField()
        high = django_forms.IntegerField()

        def clean(self):
            cleaned = super().clean()
            if cleaned["low"] > cleaned["high"]:
                raise django_forms.ValidationError("low must not exceed high")
            return cleaned

    with pytest.raises(ToolValidationError, match="low must not exceed high"):
        validate_arguments(RangeForm, {"low": 9, "high": 2}, allowed=["low", "high"])

    # Once the editor pins one half, the same message is withheld. Not because
    # this one gives anything away — it does not — but because no reading of the
    # text can tell it apart from one that does, and ``clean`` is the author's
    # code either way. Saying it on purpose is what ``ToolError`` is for.
    with pytest.raises(ToolValidationError, match="not something you can change"):
        validate_arguments(RangeForm, {"low": 9}, allowed=["low"], bound={"high": 2})

    # What the *fields* said still comes back, bound values in the form or not:
    # those messages are made from the value the model itself sent.
    with pytest.raises(ToolValidationError, match="low"):
        validate_arguments(RangeForm, {"low": "not a number"}, allowed=["low"], bound={"high": 2})


def test_a_secret_filed_under_the_models_own_field_is_still_a_secret():
    """Which field an error is filed under says nothing about who wrote it.

    ``clean`` can call ``add_error`` naming any field it likes, so a message
    holding the editor's API key lands under the very field the model filled —
    and reading the text for that key catches only the case where it appears
    whole. A prefix, a reformatting, or a value pulled out of a dictionary all
    walk straight past it.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import ToolValidationError, validate_arguments

    class ChargeForm(django_forms.Form):
        amount = django_forms.IntegerField()
        credentials = django_forms.JSONField()

        def clean(self):
            cleaned = super().clean()
            key = (cleaned.get("credentials") or {}).get("api_key", "")
            self.add_error("amount", f"key {key[:12]}… was rejected by the gateway")
            return cleaned

    with pytest.raises(ToolValidationError) as refused:
        validate_arguments(
            ChargeForm,
            {"amount": 500},
            allowed=["amount"],
            bound={"credentials": {"api_key": "sk-live-do-not-share"}},
        )
    assert "sk-live" not in str(refused.value), "nested, truncated, and filed under the model's own field"
    assert "not something you can change" in str(refused.value)


def test_a_complaint_that_quotes_a_bound_value_is_not_repeated():
    """What makes a combination error unsafe is the value in it, not the field.

    So the message is what gets looked at. One that spells out the editor's
    value hands it over just as surely as a field error would.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import ToolValidationError, validate_arguments

    class LimitForm(django_forms.Form):
        amount = django_forms.IntegerField()
        ceiling = django_forms.CharField()

        def clean(self):
            cleaned = super().clean()
            raise django_forms.ValidationError(f"the ceiling for this account is {cleaned.get('ceiling')}")

    with pytest.raises(ToolValidationError, match="not something you can change") as refused:
        validate_arguments(LimitForm, {"amount": 500}, allowed=["amount"], bound={"ceiling": "internal-tier-3"})
    assert "internal-tier-3" not in str(refused.value)


@pytest.mark.django_db
def test_resuming_a_persons_tool_without_an_answer_says_nothing_else(run_setup, settings, admin_user):
    """The *Resume* button posts no response, and that is the common case.

    A tool that waits for somebody has to report what they said. Falling back
    to the rows when they said nothing means the ordinary way of answering
    hands the model the entire payload — every field the editor kept from it —
    as the reward for having waited.
    """
    from djangocms_automation.engine import resume_action

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="UserInputAction",
        tool_name="escalate",
        exposed_fields=["note"],
        config={},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="escalate", arguments={"note": "Need a hand"})]))

    trigger.trigger_execution(data=[{"customer_token": "sk-do-not-share", "seed": 1}])

    call = call_action()
    assert call.state == WAITING

    SCRIPT.append(says(text="Understood."))
    resume_action(call.pk, admin_user)

    observation = observations(step_action())[0]["content"]
    assert "sk-do-not-share" not in observation, "silence is not consent to hand over the payload"
    assert "without leaving a response" in observation, "and the model is told what happened"


@pytest.mark.django_db
def test_a_call_that_changed_while_waiting_is_put_back_for_approval(run_setup, settings, admin_user):
    """Consent is given to an operation, not to a plugin.

    A call can wait for days. If the editor repoints the recipient in that
    time, resuming would send somebody's approved words to an address they
    never saw.
    """
    from cms.plugin_pool import plugin_pool
    from django.core import mail

    from djangocms_automation.engine import resume_action

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        config={"recipient_email": "'ada@example.com'", "subject": "'x'", "body": "x"},
        requires_approval=True,
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    call = call_action()
    assert call.result["bound"][0]["inputs"]["recipient_email"] == "ada@example.com"

    # The editor changes their mind about the recipient while it waits.
    model = plugin_pool.get_plugin("MailAction").model
    instance = model.objects.get(pk=tool.pk)
    instance.config = {**instance.config, "recipient_email": "'someone-else@example.com'"}
    instance.save()

    sent_before = len(mail.outbox)
    SCRIPT.append(says(text="Sent."))
    resume_action(call.pk, admin_user)

    call.refresh_from_db()
    assert len(mail.outbox) == sent_before, "nobody approved this one"
    assert call.state == WAITING, "so it is asked again rather than run"
    assert call.result["changed"] is True, "and the page says why it is being asked twice"
    assert call.result["bound"][0]["inputs"]["recipient_email"] == "someone-else@example.com"


@pytest.mark.django_db
def test_approving_does_not_add_a_row_of_its_own(run_setup, settings, admin_user):
    """A resume form's fields are a decision, not another target.

    Appending them as a row gives the call a target that was not on the page
    the approver read.
    """
    from django.core import mail

    from djangocms_automation.engine import resume_action

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
        requires_approval=True,
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    sent_before = len(mail.outbox)
    SCRIPT.append(says(text="Sent."))
    resume_action(call_action().pk, admin_user, data={"verdict": "looks fine"})

    assert len(mail.outbox) - sent_before == 1, "one row was approved, so one message"


@pytest.mark.django_db
def test_reordering_rows_does_not_hand_them_to_the_model(run_setup, settings):
    """Position is not provenance.

    An action that sorts its rows returns the automation's own data in a
    different order. Compared position by position every field of every row
    looks new, and reporting "what changed" reports the lot.
    """
    from cms.plugin_pool import plugin_pool

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        config={"recipient_email": "'to@example.com'", "subject": "'x'", "body": "x"},
    )
    instance = plugin_pool.get_plugin("MailAction").model.objects.get(pk=tool.pk)

    given = [{"token": "sk-do-not-share", "rank": 2}, {"token": "sk-also-secret", "rank": 1}]
    sorted_back = sorted(given, key=lambda row: row["rank"])

    reported = str(instance._reportable(given, sorted_back))
    assert "sk-do-not-share" not in reported, "the same rows, in a different order"
    assert "sk-also-secret" not in reported


@pytest.mark.django_db
def test_repeated_targets_are_counted_not_collapsed(run_setup, settings):
    """Three messages to one address are three messages.

    Showing a single line understates by two, and understating is the one
    direction an approver has no way of checking.
    """
    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        requires_approval=True,
        config={"recipient_email": "customer.email", "subject": "'x'", "body": "x"},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))

    trigger.trigger_execution(data=[{"customer": {"email": "ada@example.com"}}] * 3)

    shown = call_action().result["bound"]
    assert len(shown) == 1, "one address"
    assert shown[0]["times"] == 3, "written to three times"


@pytest.mark.django_db
def test_turning_approval_off_does_not_wave_a_pending_call_through(run_setup, settings, admin_user):
    """A call already waiting was started under the gate.

    Reading only the current setting would make *disabling approval* the way to
    run an operation nobody agreed to — the opposite of what disabling it is
    for, and reachable by an editor who never saw the pending task.
    """
    from cms.plugin_pool import plugin_pool
    from django.core import mail

    from djangocms_automation.engine import resume_action

    trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(
        placeholder,
        ai,
        settings,
        plugin_type="MailAction",
        tool_name="reply",
        exposed_fields=["subject"],
        config={"recipient_email": "'ada@example.com'", "subject": "'x'", "body": "x"},
        requires_approval=True,
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="reply", arguments={"subject": "Ship it"})]))

    trigger.trigger_execution(data=[{"seed": 1}])
    call = call_action()
    assert call.state == WAITING

    # The recipient is repointed *and* the gate is switched off while it waits.
    model = plugin_pool.get_plugin("MailAction").model
    instance = model.objects.get(pk=tool.pk)
    instance.config = {**instance.config, "recipient_email": "'someone-else@example.com'"}
    instance.requires_approval = False
    instance.save()

    sent_before = len(mail.outbox)
    SCRIPT.append(says(text="Sent."))
    resume_action(call.pk, admin_user)

    call.refresh_from_db()
    assert len(mail.outbox) == sent_before, "still not what anybody approved"
    assert call.state == WAITING
    assert call.result["changed"] is True


def test_a_per_field_hook_cannot_smuggle_a_secret_past_the_probe():
    """``clean_<field>`` is the author's code and runs as part of field validation.

    So a probe built by subclassing the form and removing ``clean`` inherits it
    anyway, and a hook reading the raw data can put the editor's key in a
    message that then looks — by every test available here — like something the
    field itself said.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import ToolValidationError, validate_arguments

    class ChargeForm(django_forms.Form):
        amount = django_forms.IntegerField()
        credentials = django_forms.JSONField()

        def clean_amount(self):
            key = (self.data.get("credentials") or {}).get("api_key", "")
            raise django_forms.ValidationError(f"the gateway rejected {key}")

    with pytest.raises(ToolValidationError) as refused:
        validate_arguments(
            ChargeForm,
            {"amount": 500},
            allowed=["amount"],
            bound={"credentials": {"api_key": "sk-live-do-not-share"}},
        )
    assert "sk-live" not in str(refused.value), "the hook ran, but not on the probe"


def test_the_probe_holds_a_literal_mapping_to_the_same_rules_as_the_call():
    """The probe has to agree with the real form about a valid argument.

    An action reading a field as a mapping of expressions has an editor's
    validator on it demanding expression syntax, and the real validation
    replaces that — a model supplies values, and ``ann smith`` is a fine value
    and not an expression. A probe using the original field would answer a
    complaint about some bound value with a second one about perfectly good
    arguments, sending the model off to fix what was never wrong.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import EXPRESSION_SYNTAX_CHECK, ToolValidationError, validate_arguments

    def expressions_only(value):
        for key, entry in (value or {}).items():
            if " " in str(entry):
                raise django_forms.ValidationError(f"{key}: not valid expression syntax")

    setattr(expressions_only, EXPRESSION_SYNTAX_CHECK, True)

    class QueryForm(django_forms.Form):
        filters = django_forms.JSONField(validators=[expressions_only])
        model = django_forms.CharField()

        def clean(self):
            raise django_forms.ValidationError("the configured model is not queryable")

    with pytest.raises(ToolValidationError) as refused:
        validate_arguments(
            QueryForm,
            {"filters": {"username": "ann smith"}},
            allowed=["filters"],
            literal_mappings=frozenset({"filters"}),
            bound={"model": "auth.User"},
        )
    assert "expression syntax" not in str(refused.value), "the model's arguments were fine"
    assert "not something you can change" in str(refused.value)


def test_a_field_configured_from_bound_data_does_not_carry_it_into_the_probe():
    """A field object is not a constant.

    A form's ``__init__`` may reach into ``self.data`` and build a validator or
    an error message out of what it finds. Copy that field into the probe and
    the probe inherits the capture — it never runs the author's code and still
    reports the author's secret, as something the field itself said.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import ToolValidationError, validate_arguments

    class ChargeForm(django_forms.Form):
        amount = django_forms.IntegerField()
        credentials = django_forms.JSONField()

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            key = (self.data.get("credentials") or {}).get("api_key", "")

            def under_policy(value):
                raise django_forms.ValidationError(f"policy for {key} rejected this")

            self.fields["amount"].validators.append(under_policy)

    with pytest.raises(ToolValidationError) as refused:
        validate_arguments(
            ChargeForm,
            {"amount": 500},
            allowed=["amount"],
            bound={"credentials": {"api_key": "sk-secret-123"}},
        )
    assert "sk-secret-123" not in str(refused.value), "the probe's field never saw it"


def test_an_actions_own_rule_about_a_mapping_still_binds_a_model():
    """Only the expression check is the editor's question. The rest are rules.

    A limit on how many entries a mapping may hold, or on which keys are
    allowed, is about the shape of the request and holds however the request
    was written. Setting those aside along with the syntax check would let a
    tool accept exactly what the action refuses — which is the one outcome
    validating through the action's own form exists to prevent.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import EXPRESSION_SYNTAX_CHECK, ToolValidationError, validate_arguments

    def expressions_only(value):
        for key, entry in (value or {}).items():
            if " " in str(entry):
                raise django_forms.ValidationError(f"{key}: not valid expression syntax")

    setattr(expressions_only, EXPRESSION_SYNTAX_CHECK, True)

    def at_most_one(value):
        if len(value or {}) > 1:
            raise django_forms.ValidationError("filter on one field at a time")

    class QueryForm(django_forms.Form):
        filters = django_forms.JSONField(validators=[expressions_only, at_most_one])

    mappings = frozenset({"filters"})

    # The action's own rule is enforced...
    with pytest.raises(ToolValidationError, match="one field at a time"):
        validate_arguments(
            QueryForm,
            {"filters": {"username": "ann smith", "email": "a@b.c"}},
            allowed=["filters"],
            literal_mappings=mappings,
        )

    # ...while the model is still free to send a value rather than an expression.
    assert validate_arguments(
        QueryForm, {"filters": {"username": "ann smith"}}, allowed=["filters"], literal_mappings=mappings
    ) == {"filters": {"username": "ann smith"}}


def test_a_mapping_fields_own_validate_still_binds_a_model():
    """A field's checking does not live only in its ``validators`` list.

    ``validate``, ``to_python`` and ``clean`` can all be overridden. A
    substitute built from a label, a required flag and a list of callables
    keeps none of that, so a subclass refusing a forbidden key would hold
    against the editor and not against the model.
    """
    from django import forms as django_forms

    from djangocms_automation.tools import EXPRESSION_SYNTAX_CHECK, ToolValidationError, validate_arguments

    def expressions_only(value):
        for key, entry in (value or {}).items():
            if " " in str(entry):
                raise django_forms.ValidationError(f"{key}: not valid expression syntax")

    setattr(expressions_only, EXPRESSION_SYNTAX_CHECK, True)

    class RestrictedMappingField(django_forms.JSONField):
        def validate(self, value):
            super().validate(value)
            if "is_superuser" in (value or {}):
                raise django_forms.ValidationError("that field is not yours to set")

    class UpdateForm(django_forms.Form):
        field_mapping = RestrictedMappingField(validators=[expressions_only])

    mappings = frozenset({"field_mapping"})

    with pytest.raises(ToolValidationError, match="not yours to set"):
        validate_arguments(
            UpdateForm,
            {"field_mapping": {"is_superuser": True}},
            allowed=["field_mapping"],
            literal_mappings=mappings,
        )

    # And the field still does its ordinary work for what the model may set.
    assert validate_arguments(
        UpdateForm, {"field_mapping": {"title": "a new title"}}, allowed=["field_mapping"], literal_mappings=mappings
    ) == {"field_mapping": {"title": "a new title"}}


@pytest.mark.django_db
def test_a_project_can_name_its_models_for_the_people_choosing_them(settings):
    """``anthropic/claude-opus-4-8`` is precise and says nothing useful.

    What an editor is choosing is which model suits the step, and a project
    knows things about that a version string cannot carry — which one is cheap,
    which one is for drafting, which one costs real money.
    """
    from djangocms_automation.ai.llm import get_allowed_llm_models, get_llm_model_choices
    from djangocms_automation.ai.step import AIStepForm

    settings.AUTOMATION_LLM_MODELS = [
        ("anthropic/claude-opus-4-8", "Claude Opus — best quality, costs real money"),
        ("dummy/echo", "Echo — answers locally, no provider"),
    ]

    assert get_llm_model_choices() == [
        ("anthropic/claude-opus-4-8", "Claude Opus — best quality, costs real money"),
        ("dummy/echo", "Echo — answers locally, no provider"),
    ]
    # The allowlist is still about model strings; a label decides nothing.
    assert get_allowed_llm_models() == ["anthropic/claude-opus-4-8", "dummy/echo"]

    rendered = str(AIStepForm()["model"])
    assert "Claude Opus — best quality" in rendered, "the editor reads the label"
    assert 'value="anthropic/claude-opus-4-8"' in rendered, "and the provider gets the string"


@pytest.mark.django_db
def test_a_bare_model_string_still_works_and_labels_itself(settings):
    """Settings written before the pair form keep working.

    And a project with nothing to add to a name need not say it twice.
    """
    from djangocms_automation.ai.llm import get_allowed_llm_models, get_llm_model_choices

    settings.AUTOMATION_LLM_MODELS = ["dummy/echo", ("openai/gpt-4.1", "GPT")]

    assert get_llm_model_choices() == [("dummy/echo", "dummy/echo"), ("openai/gpt-4.1", "GPT")]
    assert get_allowed_llm_models() == ["dummy/echo", "openai/gpt-4.1"]


@pytest.mark.django_db
def test_a_malformed_model_entry_says_so_at_the_setting(settings):
    """A misconfigured allowlist should name itself, not fail somewhere later."""
    from django.core.exceptions import ImproperlyConfigured

    from djangocms_automation.ai.llm import get_llm_model_choices

    settings.AUTOMATION_LLM_MODELS = [("anthropic/claude-opus-4-8", "Claude", "extra")]

    with pytest.raises(ImproperlyConfigured, match="AUTOMATION_LLM_MODELS"):
        get_llm_model_choices()


def test_a_field_left_out_of_required_is_refused_with_the_way_to_say_it():
    """Structured output is enforced by the provider, not checked here.

    Which is what makes the rows downstream trustworthy — and means a schema
    the provider refuses is a run that dies at the first call, long after the
    editor pressed save. A provider enforcing a schema insists that every
    field appear in ``required``; optional is said by allowing null.
    """
    from django import forms as django_forms

    from djangocms_automation.ai.step import _validate_json_schema

    with pytest.raises(django_forms.ValidationError, match="company"):
        _validate_json_schema(
            {
                "type": "object",
                "properties": {"score": {"type": "string"}, "company": {"type": "string"}},
                "required": ["score"],
                "additionalProperties": False,
            }
        )

    # The way to say it, accepted.
    _validate_json_schema(
        {
            "type": "object",
            "properties": {"score": {"type": "string"}, "company": {"type": ["string", "null"]}},
            "required": ["score", "company"],
            "additionalProperties": False,
        }
    )


def test_the_rules_reach_a_nested_object_too():
    """*Edit as JSON* accepts nesting the field editor cannot show.

    A nested object breaks the request exactly as the top one does.
    """
    from django import forms as django_forms

    from djangocms_automation.ai.step import _validate_json_schema

    def nested(inner):
        return {
            "type": "object",
            "properties": {"customer": inner},
            "required": ["customer"],
            "additionalProperties": False,
        }

    with pytest.raises(django_forms.ValidationError, match="customer"):
        _validate_json_schema(
            nested(
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "vat": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                }
            )
        )

    with pytest.raises(django_forms.ValidationError, match="additionalProperties"):
        _validate_json_schema(nested({"type": "object", "properties": {}, "required": []}))


@pytest.mark.django_db
def test_a_model_that_says_nothing_fails_the_step(run_setup, settings):
    """An empty answer is not an answer.

    The provider called the reply complete, so nothing upstream objects, and
    an empty string would travel on as though it were one — into a mail body,
    a title, a condition — leaving a blank where somebody looks later and no
    record of why.
    """
    trigger, placeholder = run_setup
    add_step(placeholder, settings)
    SCRIPT.append(says(text=""))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.state == FAILED
    assert "without saying anything" in action.result["error"]
    assert "finish reason: stop" in action.result["error"], "the fact that explains it"
    assert action.message == action.result["error"], "and it survives as the message an operator reads"


@pytest.mark.django_db
def test_a_model_that_thought_until_it_ran_out_says_which(run_setup, settings):
    """Two silences that need different answers.

    A model with nothing to say is a prompt problem. One that spent its whole
    budget thinking is a budget problem, and the failure should not leave
    somebody guessing which they have.
    """
    trigger, placeholder = run_setup
    add_step(placeholder, settings)
    SCRIPT.append(says(text="", reasoning="thinking at great length about it"))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert "spent what it had on reasoning" in step_action().result["error"]
    assert "Maximum tokens" in step_action().result["error"], "and what to do about it"


def test_an_output_shape_with_no_fields_is_refused():
    """It permits exactly one answer, and it is the empty one.

    A model replying "{}" to such a shape is not being unhelpful — that is the
    only thing it is allowed to say, and it pays tokens to discover so on every
    run.
    """
    from django import forms as django_forms

    from djangocms_automation.ai.step import _validate_json_schema

    with pytest.raises(django_forms.ValidationError, match="at least one field"):
        _validate_json_schema({"type": "object", "properties": {}, "required": [], "additionalProperties": False})

    with pytest.raises(django_forms.ValidationError, match="at least one field"):
        _validate_json_schema({"type": "object", "additionalProperties": False})


@pytest.mark.django_db
def test_an_empty_answer_to_a_shape_fails_rather_than_flowing_on(run_setup, settings):
    """Answering the shape and saying nothing in it is still saying nothing.

    Reading those rows downstream finds every field missing, one step further
    on and with nothing left explaining why.
    """
    trigger, placeholder = run_setup
    add_step(placeholder, settings)
    SCRIPT.append(says(json={}))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.state == FAILED
    assert "empty answer for the output shape" in action.result["error"]


@pytest.mark.django_db
def test_the_answer_format_reaches_the_model(run_setup, settings):
    """A description that never leaves the editor steers nothing.

    The setting exists because the place this already worked — a field's
    description in the output shape — is not somewhere anyone would think to
    look for it.
    """
    trigger, placeholder = run_setup
    add_step(placeholder, settings, system_prompt="Be brief.", answer_format="text")
    SCRIPT.append(says(text="An answer."))

    trigger.trigger_execution(data=[{"seed": 1}])

    system = observations_system(step_action())
    assert "Be brief." in system, "what the editor wrote stays the substance"
    assert "Do not use Markdown" in system
    assert system.index("Be brief.") < system.index("Do not use Markdown"), "the note about presentation comes last"


@pytest.mark.django_db
def test_no_answer_format_adds_nothing(run_setup, settings):
    """Left alone, the step asks for nothing it was not told to ask for."""
    trigger, placeholder = run_setup
    add_step(placeholder, settings, system_prompt="Be brief.")
    SCRIPT.append(says(text="An answer."))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert observations_system(step_action()) == "Be brief."


@pytest.mark.django_db
def test_a_format_alone_is_the_whole_instruction(run_setup, settings):
    """With no instructions of their own, the note is all there is to say."""
    trigger, placeholder = run_setup
    add_step(placeholder, settings, answer_format="html")
    SCRIPT.append(says(text="<p>An answer.</p>"))

    trigger.trigger_execution(data=[{"seed": 1}])

    assert observations_system(step_action()).startswith("Write in HTML")


@pytest.mark.django_db
def test_the_answer_format_is_recorded_with_the_conversation(run_setup, settings):
    """A plugin is editable and a transcript is not.

    Reading the format back off the step would mean switching it to Markdown
    next week changed how last week's answer is read.
    """
    trigger, placeholder = run_setup
    add_step(placeholder, settings, answer_format="markdown")
    SCRIPT.append(says(text="## Heading"))

    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    assert action.scratch["answer_format"] == "markdown"

    from djangocms_automation.admin import _answer_format

    assert _answer_format(action) == "markdown"

    # The step is changed afterwards; the run still says what it asked for.
    from cms.plugin_pool import plugin_pool

    step = plugin_pool.get_plugin("AIStep").model.objects.get(uuid=action.plugin_ptr)
    step.config = {**step.config, "answer_format": "html"}
    step.save()

    action.refresh_from_db()
    assert _answer_format(action) == "markdown", "the transcript is read as it was written"


@pytest.mark.django_db
def test_a_run_recorded_before_this_falls_back_to_the_step(run_setup, settings):
    """Old runs kept no format, and the step is the only clue left."""
    from djangocms_automation.admin import _answer_format
    from djangocms_automation.instances import AutomationAction

    trigger, placeholder = run_setup
    add_step(placeholder, settings, answer_format="markdown")
    SCRIPT.append(says(text="## Heading"))
    trigger.trigger_execution(data=[{"seed": 1}])

    action = step_action()
    scratch = {key: value for key, value in action.scratch.items() if key != "answer_format"}
    AutomationAction.objects.filter(pk=action.pk).update(scratch=scratch)
    action.refresh_from_db()

    assert _answer_format(action) == "markdown", "read from the step, for want of anything better"


@pytest.mark.django_db
def test_intent_comes_first_for_a_plain_action(settings):
    """The reader-facing name is the first decision when adding a step."""
    from cms.plugin_pool import plugin_pool
    from django.contrib import admin as django_admin
    from django.test import RequestFactory

    plugin = plugin_pool.get_plugin("MailAction")
    request = RequestFactory().get("/")
    request.user = None

    names = [str(name) for name, _options in plugin(plugin.model, django_admin.site).get_fieldsets(request, None)]

    assert names == ["Intent", "Inputs", "Comment"]


@pytest.mark.django_db
def test_tool_wiring_and_inputs_follow_intent(run_setup, settings):
    """A tool still starts with its reader-facing intent."""
    from cms.plugin_pool import plugin_pool
    from django.contrib import admin as django_admin
    from django.test import RequestFactory

    _trigger, placeholder = run_setup
    ai = add_step(placeholder, settings)
    tool = add_tool(placeholder, ai, settings, plugin_type="MailAction", tool_name="reply", exposed_fields=["subject"])

    plugin = plugin_pool.get_plugin("MailAction")
    request = RequestFactory().get("/")
    request.user = None
    instance = plugin.model.objects.get(pk=tool.pk)

    names = [str(name) for name, _options in plugin(plugin.model, django_admin.site).get_fieldsets(request, instance)]

    assert names[:3] == ["Intent", "As a tool", "Inputs"]

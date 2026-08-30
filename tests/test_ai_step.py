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
from django.contrib.contenttypes.models import ContentType

from djangocms_automation.ai import llm, step
from djangocms_automation.ai.budget import AgentBudget, BudgetExceeded
from djangocms_automation.ai.llm import LLMResult
from djangocms_automation.ai.state import AgentState
from djangocms_automation.instances import COMPLETED, FAILED, WAITING, AutomationAction
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
from djangocms_automation.tools import ToolCall

SCRIPT: list = []


def says(text="", calls=(), usage=None, finish_reason=None, json=None):
    return LLMResult(
        text=text,
        json=json,
        model="anthropic/claude-opus-4-8",
        usage=usage or {"input_tokens": 10, "output_tokens": 5},
        tool_calls=list(calls),
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
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
    return add_plugin(
        placeholder=placeholder,
        plugin_type="AIStep",
        language=settings.LANGUAGE_CODE,
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
    add_tool(placeholder, ai, settings, plugin_type="MailAction", tool_name="reply", requires_approval=None)

    instance = step.AIStepPluginModel.objects.get(pk=ai.pk)
    request = RequestFactory().get("/")
    request.user = None
    html = render_to_string(
        "djangocms_automation/plugins/ai_step.html",
        {"instance": instance, "title": "Ask a Model", "tools": list(instance.cmsplugin_set.all())},
        request=request,
    )

    assert 'class="modifier tool"' in html, "the same row a modifier gets"
    assert "tool-marker" in html, "and a square marker rather than a round one"
    assert "bi-envelope-at" in html, "carrying the icon of the action it runs"
    assert "bi-shield-exclamation" in html, "and the gate an irreversible action gets automatically"


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
        {"instance": step.AIStepPluginModel.objects.get(pk=ai.pk), "title": "Ask a Model", "tools": []},
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
        "MailAction", ai, settings, data={"subject": "", "body": "b", "recipient_email": "r", "comment": ""}
    )
    assert not missing.is_valid()
    assert "subject" in missing.errors

    _plugin, filled_by_model = wired_form(
        "MailAction",
        ai,
        settings,
        data={
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
    assert labels == ["As a tool", "Inputs", "Comment"]


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

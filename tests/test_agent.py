"""The Agent and Tool nodes (phase 1.4 and 1.5).

Every provider reply here is scripted. That is not only to avoid the network:
the interesting behaviour of an agent is what it does when a model asks for a
tool that does not exist, never stops asking, or asks for something a person has
to approve first — none of which a live provider will do on demand.
"""

from unittest import mock

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from django.contrib.contenttypes.models import ContentType

from djangocms_automation.ai import llm
from djangocms_automation.ai.budget import AgentBudget, BudgetExceeded
from djangocms_automation.ai.llm import LLMResult, ToolCall
from djangocms_automation.ai.state import AgentState
from djangocms_automation.instances import (
    COMPLETED,
    FAILED,
    WAITING,
    AutomationAction,
    AutomationInstance,
)
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger

#: Scripted replies, consumed in order by the fake provider.
SCRIPT: list = []


def says(text="", calls=(), usage=None):
    return LLMResult(
        text=text,
        json=None,
        model="anthropic/claude-opus-4-8",
        usage=usage or {"input_tokens": 10, "output_tokens": 5},
        tool_calls=list(calls),
        finish_reason="tool_calls" if calls else "stop",
    )


@pytest.fixture(autouse=True)
def scripted_model():
    """Replace the provider with a queue of replies."""
    SCRIPT.clear()

    def complete(**kwargs):
        assert SCRIPT, "the agent asked for more turns than the test scripted"
        return SCRIPT.pop(0)

    with mock.patch.object(llm, "complete", side_effect=complete):
        yield SCRIPT


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Agent", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Agent")


@pytest.fixture
def run_setup(automation_content, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    settings.AUTOMATION_LLM_MODELS = ["anthropic/claude-opus-4-8"]
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="click", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    return trigger, placeholder


def build_agent(placeholder, settings, tools=(("echo", "ActionPlugin", []),), **kwargs):
    """An agent with one tool per entry: (name, wrapped plugin, exposed fields)."""
    agent = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationAgent",
        language=settings.LANGUAGE_CODE,
        model="anthropic/claude-opus-4-8",
        prompt="do the thing",
        **kwargs,
    )
    for name, wrapped, exposed in tools:
        tool = add_plugin(
            placeholder=placeholder,
            plugin_type="AutomationAgentTool",
            language=settings.LANGUAGE_CODE,
            target=agent,
            tool_name=name,
            tool_description=f"The {name} tool.",
            exposed_fields=exposed,
        )
        add_plugin(placeholder=placeholder, plugin_type=wrapped, language=settings.LANGUAGE_CODE, target=tool)
    return agent


def agent_action():
    return AutomationAction.objects.filter(parent__isnull=True).latest("id")


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_agent_that_needs_no_tools_answers_immediately(run_setup, settings):
    trigger, placeholder = run_setup
    build_agent(placeholder, settings)
    SCRIPT.append(says(text="Nothing to do."))

    trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.state == COMPLETED
    assert agent.children.count() == 0
    assert AutomationInstance.objects.latest("id").data[0]["text"] == "Nothing to do."


@pytest.mark.django_db
def test_a_tool_call_becomes_an_action_and_the_agent_goes_round(run_setup, settings):
    """The property the whole design rests on: a tool call is a real step."""
    trigger, placeholder = run_setup
    build_agent(placeholder, settings)
    SCRIPT.extend(
        [
            says(calls=[ToolCall(id="c1", name="echo", arguments={})]),
            says(text="Done."),
        ]
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.state == COMPLETED
    assert agent.children.count() == 1, "the tool call ran as its own action"
    assert agent.children.first().state == COMPLETED
    assert AutomationInstance.objects.latest("id").data[0]["text"] == "Done."


@pytest.mark.django_db
def test_turns_are_re_entries_not_attempts(run_setup, settings):
    """An agent re-claims its own action every turn. Counted as attempts, any
    agent would exhaust its retry budget for working correctly."""
    trigger, placeholder = run_setup
    build_agent(placeholder, settings)
    SCRIPT.extend(
        [
            says(calls=[ToolCall(id="c1", name="echo", arguments={})]),
            says(calls=[ToolCall(id="c2", name="echo", arguments={})]),
            says(text="Done."),
        ]
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.attempt_count == 1, "taking a turn is not retrying"
    assert agent.re_entry_count >= 2
    assert agent.dead_lettered is False


@pytest.mark.django_db
def test_the_conversation_is_kept_across_turns(run_setup, settings):
    """Each turn is a separate process in production, so the transcript has to
    come back from the database."""
    trigger, placeholder = run_setup
    build_agent(placeholder, settings)
    SCRIPT.extend(
        [
            says(calls=[ToolCall(id="c1", name="echo", arguments={})]),
            says(text="Done."),
        ]
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    state = AgentState.load(agent_action())
    roles = [message["role"] for message in state.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert state.tool_calls == 1


@pytest.mark.django_db
def test_a_tool_the_agent_does_not_have_is_reported_back_to_it(run_setup, settings):
    """A model naming a tool that does not exist is a correctable mistake."""
    trigger, placeholder = run_setup
    build_agent(placeholder, settings)
    SCRIPT.extend(
        [
            says(calls=[ToolCall(id="c1", name="nonexistent", arguments={})]),
            says(text="Recovered."),
        ]
    )

    trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.state == COMPLETED
    observation = next(m for m in AgentState.load(agent).messages if m["role"] == "tool")
    assert "No tool named" in observation["content"]
    assert "echo" in observation["content"], "it is told what it does have"


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_agent_that_never_finishes_fails_at_its_turn_limit(run_setup, settings):
    """Stopping quietly would return a confident partial answer."""
    trigger, placeholder = run_setup
    build_agent(placeholder, settings, max_turns=2)
    for index in range(6):
        SCRIPT.append(says(calls=[ToolCall(id=f"c{index}", name="echo", arguments={})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.state == FAILED
    assert "2 turns" in agent.result["error"]
    assert agent.dead_lettered is True, "it must be inspectable afterwards"


@pytest.mark.django_db
def test_the_tool_call_budget_is_enforced(run_setup, settings):
    trigger, placeholder = run_setup
    build_agent(placeholder, settings, max_tool_calls=1, max_turns=10)
    for index in range(6):
        SCRIPT.append(says(calls=[ToolCall(id=f"c{index}", name="echo", arguments={})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.state == FAILED
    assert "tool calls" in agent.result["error"]


@pytest.mark.django_db
def test_the_token_budget_is_enforced(run_setup, settings):
    trigger, placeholder = run_setup
    build_agent(placeholder, settings, max_tokens=20, max_turns=10)
    for index in range(6):
        SCRIPT.append(says(calls=[ToolCall(id=f"c{index}", name="echo", arguments={})], usage={"input_tokens": 15}))

    trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.state == FAILED
    assert "tokens" in agent.result["error"]


def test_the_budget_checks_each_limit_separately():
    """They fail differently, so they are separate numbers."""
    budget = AgentBudget(max_turns=2, max_tool_calls=3, max_tokens=100, deadline_seconds=0)

    budget.check(AgentState(turn=1, tool_calls=1, usage={"t": 10}))
    with pytest.raises(BudgetExceeded, match="turns"):
        budget.check(AgentState(turn=2))
    with pytest.raises(BudgetExceeded, match="tool calls"):
        budget.check(AgentState(turn=0, tool_calls=3))
    with pytest.raises(BudgetExceeded, match="tokens"):
        budget.check(AgentState(turn=0, usage={"t": 100}))


def test_a_long_observation_is_truncated_before_it_reaches_the_model():
    from djangocms_automation.ai.tools import ToolResult

    trimmed = ToolResult(call_id="c1", content="x" * 5000).truncate(AgentBudget().max_observation_chars)
    assert len(str(trimmed.content)) <= AgentBudget().max_observation_chars + 100


# --------------------------------------------------------------------------
# Governance
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_agent_can_only_call_the_tools_declared_inside_it(run_setup, settings):
    """There is no registry to reach past: the tools are the agent's children."""
    _trigger, placeholder = run_setup
    agent = build_agent(placeholder, settings, tools=(("echo", "ActionPlugin", []),))
    from djangocms_automation.ai.models import AgentPluginModel

    instance = AgentPluginModel.objects.get(pk=agent.pk)
    instance.child_plugin_instances = list(instance.cmsplugin_set.all())
    assert [tool.tool_name for tool in instance._tools()] == ["echo"]


@pytest.mark.django_db
def test_a_tool_needing_approval_waits_for_a_person(run_setup, settings):
    """The approval gate is the existing human-in-the-loop pause, so the call
    shows up in Open tasks with the arguments the model chose."""
    trigger, placeholder = run_setup
    build_agent(placeholder, settings)
    from djangocms_automation.ai.models import AgentToolPluginModel

    AgentToolPluginModel.objects.filter(tool_name="echo").update(requires_approval=True)
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="echo", arguments={})]))

    trigger.trigger_execution(data=[{"seed": 1}])

    call = AutomationAction.objects.filter(parent__isnull=False).latest("id")
    assert call.state == WAITING
    assert call.requires_interaction is True
    assert call.result["tool"] == "echo"
    assert agent_action().state == WAITING, "the agent waits with it"


@pytest.mark.django_db
def test_an_editor_can_relax_the_default_but_has_to_say_so(run_setup, settings):
    """The conservative default can be relaxed; the opposite mistake cannot be undone.

    Unset means "decide by what the action does"; False is an editor saying
    they know, and is left alone.
    """
    from djangocms_automation.ai.models import AgentToolPluginModel

    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("wipe", "UpdateModelAction", []),))
    tool = AgentToolPluginModel.objects.get(tool_name="wipe")
    tool.child_plugin_instances = list(tool.cmsplugin_set.all())
    assert tool.is_destructive()
    assert tool.requires_approval is None, "nothing was chosen in the editor"
    assert tool.needs_approval() is True

    tool.requires_approval = False
    assert tool.needs_approval() is False, "an explicit choice is not overruled"


@pytest.mark.django_db
def test_an_agent_with_no_tools_warns_the_editor(run_setup, settings):
    from djangocms_automation.ai.models import AgentPluginModel

    _trigger, placeholder = run_setup
    agent = build_agent(placeholder, settings, tools=())
    instance = AgentPluginModel.objects.get(pk=agent.pk)
    instance.child_plugin_instances = []
    assert any("no tools" in str(message) for message in instance.messages())


@pytest.mark.django_db
def test_two_tools_with_the_same_name_warn_the_editor(run_setup, settings):
    from djangocms_automation.ai.models import AgentPluginModel

    _trigger, placeholder = run_setup
    agent = build_agent(placeholder, settings, tools=(("echo", "ActionPlugin", []), ("echo", "ActionPlugin", [])))
    instance = AgentPluginModel.objects.get(pk=agent.pk)
    instance.child_plugin_instances = list(instance.cmsplugin_set.all())
    assert any("share the name" in str(message) for message in instance.messages())


# --------------------------------------------------------------------------
# Review findings — see the tests above for the intended behaviour
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_destructive_tool_requires_approval_when_built_in_the_normal_order(run_setup, settings):
    """A tool is saved before the action it wraps exists.

    That is simply the order the editor works in: you add the tool, then drop an
    action inside it. Deciding at save time whether the wrapped action is
    irreversible therefore always decides against a tool that wraps nothing.
    """
    from djangocms_automation.ai.models import AgentToolPluginModel

    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("wipe", "UpdateModelAction", []),))

    tool = AgentToolPluginModel.objects.get(tool_name="wipe")
    tool.child_plugin_instances = list(tool.cmsplugin_set.all())
    assert tool.get_tool_spec().requires_approval is True


@pytest.mark.django_db
def test_an_approved_call_actually_runs_the_tool(run_setup, settings, admin_user):
    """Approving a call has to run it.

    The pause is the engine's human-in-the-loop wait, and resuming one normally
    means "this step is done". For an approval it means the opposite: the step
    has not run yet, and approving is what lets it.
    """
    from djangocms_automation.ai.models import AgentToolPluginModel
    from djangocms_automation.engine import resume_action

    _trigger, placeholder = run_setup
    build_agent(placeholder, settings)
    AgentToolPluginModel.objects.filter(tool_name="echo").update(requires_approval=True)
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="echo", arguments={})]))

    _trigger.trigger_execution(data=[{"seed": 1}])

    call = AutomationAction.objects.exclude(parent__isnull=True).latest("id")
    assert call.state == WAITING, "waiting for a person"

    SCRIPT.append(says(text="Done."))
    resume_action(call.pk, admin_user)

    call.refresh_from_db()
    assert call.state == COMPLETED
    assert (call.scratch or {}).get("tool_result"), "the wrapped action ran and its result was recorded"
    observations = [m for m in AgentState.load(agent_action()).messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in observations] == ["c1"], "the model was told what the tool returned"


@pytest.mark.django_db
def test_every_requested_call_is_answered_before_the_next_turn(run_setup, settings):
    """A conversation must answer every tool call the model asked for.

    Providers reject a request whose assistant turn contains a tool call with no
    matching result. Dispatching one of two and dropping the other produces
    exactly that.
    """
    sent: list = []

    def complete(**kwargs):
        # Copied, not referenced: this is the agent's live conversation and it
        # keeps appending to it, so a reference would show the reply too.
        sent.append([dict(message) for message in kwargs.get("messages") or []])
        assert SCRIPT, "the agent asked for more turns than the test scripted"
        return SCRIPT.pop(0)

    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("echo", "ActionPlugin", []), ("ping", "ActionPlugin", [])))
    SCRIPT.append(
        says(calls=[ToolCall(id="c1", name="echo", arguments={}), ToolCall(id="c2", name="ping", arguments={})])
    )
    SCRIPT.append(says(text="Done."))

    with mock.patch.object(llm, "complete", side_effect=complete):
        _trigger.trigger_execution(data=[{"seed": 1}])

    assert agent_action().state == COMPLETED
    for messages in sent:
        requested = [
            call["id"] for m in messages if m.get("role") == "assistant" for call in m.get("tool_calls") or []
        ]
        answered = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        assert sorted(requested) == sorted(answered), f"unanswered tool calls: {messages}"


@pytest.mark.django_db
def test_a_tool_wrapping_a_human_step_waits_for_the_human(run_setup, settings):
    """An action whose behaviour lives in execute() must not be bypassed.

    Wait for User pauses from ``execute``; ``perform`` is the base pass-through.
    Calling the latter makes an escalation tool report success without anyone
    having seen it.
    """
    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("escalate", "UserInputAction", []),))
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="escalate", arguments={})]))

    _trigger.trigger_execution(data=[{"seed": 1}])

    call = AutomationAction.objects.exclude(parent__isnull=True).latest("id")
    assert call.state == WAITING
    assert call.requires_interaction, "it is a task for a person, and shows up as one"


@pytest.mark.django_db
def test_a_finished_tool_call_does_not_start_the_next_tool(run_setup, settings):
    """Tools are dispatched by their agent, never by the plugin tree.

    The default ``get_next_actions`` starts whatever plugin comes next, which
    for a tool is the tool beside it — so an agent's second tool would run
    because the first one did, with no model having asked for it.
    """
    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("echo", "ActionPlugin", []), ("ping", "ActionPlugin", [])))
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="echo", arguments={})]))
    SCRIPT.append(says(text="Done."))

    _trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.state == COMPLETED
    calls = agent.children.all()
    assert calls.count() == 1, "only the tool the model asked for ran"
    assert (calls.first().scratch or {})["tool_call"]["name"] == "echo"


@pytest.mark.django_db
def test_the_approval_form_names_the_automatic_choice(run_setup, settings):
    """ "Unknown" is Django's word for an unset boolean, and a bad one here."""
    from djangocms_automation.ai.cms_plugins import AgentToolForm

    choices = dict(AgentToolForm().fields["requires_approval"].choices)
    assert "Automatic" in str(choices[""])
    assert AgentToolForm({"requires_approval": ""}).fields["requires_approval"].clean("") is None


@pytest.mark.django_db
def test_a_turn_cannot_spend_more_tool_calls_than_it_has(run_setup, settings):
    """The budget bounds side effects, so it has to be applied before dispatch.

    Checking it only at the start of the next turn is checking it after the
    calls have already run, which is exactly what the limit exists to prevent.
    """
    _trigger, placeholder = run_setup
    build_agent(
        placeholder,
        settings,
        tools=(("echo", "ActionPlugin", []), ("ping", "ActionPlugin", [])),
        max_tool_calls=1,
    )
    SCRIPT.append(
        says(calls=[ToolCall(id="c1", name="echo", arguments={}), ToolCall(id="c2", name="ping", arguments={})])
    )
    SCRIPT.append(says(text="Done."))

    _trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.children.count() == 1, "the second call was over the limit and must not have run"
    dispatched = [m for m in AgentState.load(agent).messages if m.get("role") == "tool"]
    assert len(dispatched) == 2, "the model is still told what happened to both"


@pytest.mark.django_db
def test_an_approver_can_see_what_they_are_approving(run_setup, settings, admin_client):
    """A gate nobody can see through is not a gate.

    The point of pausing is that a person decides, and they cannot decide from
    an em dash: the page has to name the tool and show the arguments the model
    chose.
    """
    from djangocms_automation.ai.models import AgentToolPluginModel

    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("send_mail", "MailAction", ["subject"]),))
    AgentToolPluginModel.objects.filter(tool_name="send_mail").update(requires_approval=True)
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="send_mail", arguments={"subject": "Ship it"})]))

    _trigger.trigger_execution(data=[{"seed": 1}])

    page = admin_client.get("/admin/djangocms_automation/automationinstance/open-tasks/")
    body = page.content.decode()
    assert "send_mail" in body, "the tool is named"
    assert "Ship it" in body, "the arguments the model chose are shown"


@pytest.mark.django_db
def test_a_waiting_tool_keeps_the_note_written_for_the_person(run_setup, settings):
    """Wait for User exists to tell someone what is needed. Losing the note
    leaves them a task with no instructions."""
    from djangocms_automation.models import BaseActionPluginModel

    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("escalate", "UserInputAction", []),))
    BaseActionPluginModel.objects.filter(plugin_type="UserInputAction").update(
        config={"note": "Please check this refund."}
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="escalate", arguments={})]))

    _trigger.trigger_execution(data=[{"seed": 1}])

    call = AutomationAction.objects.exclude(parent__isnull=True).latest("id")
    assert call.state == WAITING
    assert call.result.get("note") == "Please check this refund."


@pytest.mark.django_db
def test_replaying_a_tool_call_keeps_the_call_it_was(run_setup, settings):
    """A replayed tool call has to be the same call.

    The engine seeds a replacement from the failed attempt's input, but a tool
    call is not in its input — it is the request the model made, held on the
    node. Without it the replacement runs an empty call and answers the
    provider with an id belonging to nothing.
    """
    from djangocms_automation.ai.models import AgentToolPluginModel
    from djangocms_automation.engine import fail_action, replay_action

    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("send_mail", "MailAction", ["subject"]),))
    AgentToolPluginModel.objects.filter(tool_name="send_mail").update(requires_approval=True)
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="send_mail", arguments={"subject": "Ship it"})]))

    _trigger.trigger_execution(data=[{"seed": 1}])

    call = AutomationAction.objects.exclude(parent__isnull=True).latest("id")
    fail_action(call, "the worker died", allowed_from=(WAITING,))
    call.refresh_from_db()
    assert call.state == FAILED and call.dead_lettered

    replacement = replay_action(call.pk)

    assert replacement.scratch["tool_call"]["id"] == "c1"
    assert replacement.scratch["tool_call"]["name"] == "send_mail"
    assert replacement.scratch["tool_call"]["arguments"] == {"subject": "Ship it"}


@pytest.mark.django_db
def test_a_replayed_call_has_to_be_approved_again(run_setup, settings):
    """Approval is granted to a call, not to a tool.

    Replaying is an operator deciding to run something again; carrying the old
    approval forward would let that happen without anyone seeing the call.
    """
    from djangocms_automation.ai.models import AgentToolPluginModel
    from djangocms_automation.engine import fail_action, replay_action

    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("send_mail", "MailAction", ["subject"]),))
    AgentToolPluginModel.objects.filter(tool_name="send_mail").update(requires_approval=True)
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="send_mail", arguments={"subject": "Ship it"})]))

    _trigger.trigger_execution(data=[{"seed": 1}])

    call = AutomationAction.objects.exclude(parent__isnull=True).latest("id")
    AutomationAction.objects.filter(pk=call.pk).update(scratch={**call.scratch, "approved": True})
    call.refresh_from_db()
    fail_action(call, "the worker died", allowed_from=(WAITING,))

    replacement = replay_action(call.pk)

    assert "approved" not in replacement.scratch
    replacement.refresh_from_db()
    assert replacement.requires_interaction, "it asks again"


@pytest.mark.django_db
def test_a_reply_cut_off_at_the_token_limit_fails_the_run(run_setup, settings):
    """A truncated answer is not an answer.

    It reads like one — it is fluent, it just stops — so nothing downstream can
    tell it apart from a complete one. The provider does say, and that is the
    only chance anybody gets to notice.
    """
    _trigger, placeholder = run_setup
    build_agent(placeholder, settings)
    reply = says(text="The refund policy is that")
    reply.finish_reason = "length"
    SCRIPT.append(reply)

    _trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.state == FAILED
    assert "cut off" in agent.result["error"] or "token limit" in agent.result["error"]


@pytest.mark.django_db
def test_a_filtered_reply_fails_the_run(run_setup, settings):
    _trigger, placeholder = run_setup
    build_agent(placeholder, settings)
    reply = says(text="")
    reply.finish_reason = "content_filter"
    SCRIPT.append(reply)

    _trigger.trigger_execution(data=[{"seed": 1}])

    assert agent_action().state == FAILED


@pytest.mark.django_db
def test_a_tool_call_cut_off_mid_arguments_is_not_run(run_setup, settings):
    """Truncation is worse for a tool call than for text.

    The arguments are JSON assembled from a stream; cut short, they parse to
    something plausible and wrong, and then a tool runs with it.
    """
    _trigger, placeholder = run_setup
    build_agent(placeholder, settings, tools=(("send_mail", "MailAction", ["subject"]),))
    reply = says(calls=[ToolCall(id="c1", name="send_mail", arguments={"subject": "Your refund of $"})])
    reply.finish_reason = "length"
    SCRIPT.append(reply)

    _trigger.trigger_execution(data=[{"seed": 1}])

    agent = agent_action()
    assert agent.state == FAILED
    assert agent.children.count() == 0, "nothing ran on truncated arguments"


@pytest.mark.django_db
def test_a_literally_configured_action_receives_the_model_s_inputs(run_setup, settings, django_user_model):
    """Exposing a field has to change what the action does.

    An action reads its inputs in one of two ways: through ``resolve_inputs``,
    which treats them as expressions, or straight off ``config``, which treats
    them as values. A tool that reaches only the first kind validates the
    model's arguments and then quietly runs the editor's configuration instead.
    """
    settings.AUTOMATION_ALLOWED_MODELS = ["auth.User"]
    for name in ("ann", "bo", "cy"):
        django_user_model.objects.create(username=name)

    _trigger, placeholder = run_setup
    agent = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationAgent",
        language=settings.LANGUAGE_CODE,
        model="anthropic/claude-opus-4-8",
        prompt="find someone",
    )
    tool = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationAgentTool",
        language=settings.LANGUAGE_CODE,
        target=agent,
        tool_name="find_users",
        tool_description="Find users.",
        exposed_fields=["limit"],
    )
    add_plugin(
        placeholder=placeholder,
        plugin_type="QueryModelAction",
        language=settings.LANGUAGE_CODE,
        target=tool,
        config={"model": "auth.User", "limit": 100},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="find_users", arguments={"limit": 1})]))
    SCRIPT.append(says(text="Found one."))

    _trigger.trigger_execution(data=[{"seed": 1}])

    call = AutomationAction.objects.exclude(parent__isnull=True).latest("id")
    assert call.state == COMPLETED
    assert len(call.result) == 1, f"the model asked for one row, got {len(call.result)}"


@pytest.mark.django_db
def test_an_expression_configured_action_still_gets_literals(run_setup, settings):
    """The other kind must keep working, and for the opposite reason.

    Its config holds expressions over the automation's data, so a value the
    model supplies has to bypass resolution — sending it through would read
    "Ship it" as a data path.
    """
    from django.core import mail

    _trigger, placeholder = run_setup
    agent = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationAgent",
        language=settings.LANGUAGE_CODE,
        model="anthropic/claude-opus-4-8",
        prompt="write to them",
    )
    tool = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationAgentTool",
        language=settings.LANGUAGE_CODE,
        target=agent,
        tool_name="send_mail",
        tool_description="Send mail.",
        exposed_fields=["subject", "body"],
        requires_approval=False,
    )
    add_plugin(
        placeholder=placeholder,
        plugin_type="MailAction",
        language=settings.LANGUAGE_CODE,
        target=tool,
        config={"subject": "'unused'", "body": "unused", "recipient_email": "'to@example.com'"},
    )
    SCRIPT.append(says(calls=[ToolCall(id="c1", name="send_mail", arguments={"subject": "Ship it", "body": "Now"})]))
    SCRIPT.append(says(text="Sent."))

    _trigger.trigger_execution(data=[{"seed": 1}])

    assert mail.outbox, "the mail was sent"
    assert mail.outbox[-1].subject == "Ship it", "the literal, not a data path"

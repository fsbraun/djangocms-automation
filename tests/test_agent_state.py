"""Agent runtime state (phase 1.3).

An agent suspends between turns, and the process that resumes it may not be the
one that suspended it. Everything an agent knows therefore has to survive a
round trip through the database — and survive a failure below it, which is why
it does not live in ``result``.
"""

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from django.contrib.contenttypes.models import ContentType

from djangocms_automation import engine
from djangocms_automation.ai.llm import LLMResult, ToolCall
from djangocms_automation.ai.state import AgentState
from djangocms_automation.instances import FAILED, AutomationAction
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Agent state", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation, description="Agent state content"
    )


@pytest.fixture
def action(automation_content, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="click", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)
    trigger.trigger_execution(data=[{"seed": 1}])
    return AutomationAction.objects.latest("id")


def reply(text="", tool_calls=(), usage=None):
    return LLMResult(
        text=text,
        json=None,
        model="anthropic/claude-opus-4-8",
        usage=usage or {},
        tool_calls=list(tool_calls),
        finish_reason="tool_calls" if tool_calls else "stop",
    )


# --------------------------------------------------------------------------
# Surviving the round trip
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_state_survives_a_save_and_load(action):
    """The whole point: the process that resumes an agent is not the one that
    suspended it, so nothing may be held in memory."""
    state = AgentState()
    state.start(system="be brief", prompt="do the thing")
    state.record_reply(reply(tool_calls=[ToolCall(id="c1", name="lookup", arguments={"q": "x"})]))
    state.queue([ToolCall(id="c1", name="lookup", arguments={"q": "x"})])
    state.save(action)

    reloaded = AgentState.load(AutomationAction.objects.get(pk=action.pk))

    assert reloaded.turn == 1
    assert reloaded.messages[0] == {"role": "system", "content": "be brief"}
    assert reloaded.messages[-1]["tool_calls"][0]["function"]["name"] == "lookup"
    assert [call.id for call in reloaded.undispatched()] == ["c1"]


@pytest.mark.django_db
def test_a_node_with_no_state_yet_loads_cleanly(action):
    state = AgentState.load(action)
    assert state.turn == 0 and state.messages == [] and state.pending == []


@pytest.mark.django_db
def test_unknown_keys_in_stored_state_are_ignored(action):
    """A row written by an older version must not crash a newer one."""
    AutomationAction.objects.filter(pk=action.pk).update(scratch={"turn": 2, "from_the_future": "?"})

    state = AgentState.load(AutomationAction.objects.get(pk=action.pk))
    assert state.turn == 2


@pytest.mark.django_db
def test_saving_state_does_not_disturb_the_rest_of_the_row(action):
    """It is written with a targeted update, not a full save of a stale object."""
    AutomationAction.objects.filter(pk=action.pk).update(message="important")

    AgentState(turn=3).save(AutomationAction.objects.get(pk=action.pk))

    refreshed = AutomationAction.objects.get(pk=action.pk)
    assert refreshed.scratch["turn"] == 3
    assert refreshed.message == "important"


# --------------------------------------------------------------------------
# Why not `result`
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_scratch_survives_a_failure_propagating_through_the_node(action, automation_content):
    """The reason agent state gets a field of its own.

    Failure propagation overwrites ``result``. Both the loop and the conditional
    have to derive their state from the action tree to work around that; a
    conversation cannot be derived from anything, so it needs somewhere the
    engine will not write.
    """
    parent = AutomationAction.objects.create(
        automation_instance=action.automation_instance,
        plugin_ptr=action.plugin_ptr,
        state="WAITING",
        finished=None,
    )
    AutomationAction.objects.filter(pk=action.pk).update(parent=parent, state="RUNNING", finished=None)
    AgentState(turn=4, messages=[{"role": "user", "content": "remember me"}]).save(parent)

    engine.propagate_failure(AutomationAction.objects.get(pk=action.pk))

    parent.refresh_from_db()
    assert parent.state == FAILED
    assert parent.result == {"failed_action_id": action.pk}, "result is the engine's to overwrite"
    assert AgentState.load(parent).messages == [{"role": "user", "content": "remember me"}]


# --------------------------------------------------------------------------
# The conversation
# --------------------------------------------------------------------------


def test_a_tool_request_is_kept_verbatim_in_the_conversation():
    """Providers reject a transcript whose tool results answer nothing."""
    state = AgentState()
    state.record_reply(reply(text="looking", tool_calls=[ToolCall(id="c1", name="lookup", arguments={"q": "x"})]))
    state.record_observation("c1", "found it")

    assistant, tool = state.messages[-2], state.messages[-1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "c1"
    assert tool == {"role": "tool", "tool_call_id": "c1", "content": "found it"}


def test_an_error_observation_says_so():
    """A failed tool call is something the model should try to correct."""
    state = AgentState()
    state.record_observation("c1", "Unknown argument: city", is_error=True)
    assert state.messages[-1]["content"].startswith("Error: ")


def test_usage_accumulates_across_turns():
    state = AgentState()
    state.record_reply(reply(usage={"input_tokens": 10, "output_tokens": 5}))
    state.record_reply(reply(usage={"input_tokens": 7, "output_tokens": 3}))

    assert state.usage == {"input_tokens": 17, "output_tokens": 8}
    assert state.total_tokens == 25
    assert state.turn == 2


def test_unusable_arguments_do_not_break_the_transcript():
    """Whatever the model produced, the conversation must stay serializable."""
    state = AgentState()
    state.record_reply(reply(tool_calls=[ToolCall(id="c1", name="x", arguments={"bad": {1, 2}})]))
    assert state.messages[-1]["tool_calls"][0]["function"]["arguments"] == "{}"


# --------------------------------------------------------------------------
# Dispatch bookkeeping
# --------------------------------------------------------------------------


def test_a_dispatched_call_is_not_dispatched_again():
    """The difference between resuming an agent and duplicating a side effect."""
    calls = [ToolCall(id="c1", name="a", arguments={}), ToolCall(id="c2", name="b", arguments={})]
    state = AgentState()
    state.queue(calls)

    assert [call.id for call in state.undispatched()] == ["c1", "c2"]
    state.mark_dispatched([calls[0]])
    assert [call.id for call in state.undispatched()] == ["c2"]
    state.mark_dispatched(calls)
    assert state.undispatched() == []


def test_marking_the_same_call_twice_is_harmless():
    call = ToolCall(id="c1", name="a", arguments={})
    state = AgentState()
    state.queue([call])
    state.mark_dispatched([call])
    state.mark_dispatched([call])
    assert state.dispatched == ["c1"]


@pytest.mark.django_db
def test_dispatch_bookkeeping_survives_the_round_trip(action):
    """The check happens after a wake-up, so it has to come back from the database."""
    calls = [ToolCall(id="c1", name="a", arguments={}), ToolCall(id="c2", name="b", arguments={})]
    state = AgentState()
    state.queue(calls)
    state.mark_dispatched([calls[0]])
    state.save(action)

    reloaded = AgentState.load(AutomationAction.objects.get(pk=action.pk))
    assert [call.id for call in reloaded.undispatched()] == ["c2"]

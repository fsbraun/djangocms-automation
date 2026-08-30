"""The AI step: ask a model, and let it use the actions you give it.

One plugin, and what it can do depends on what is inside it. With no tools it
asks a model a question and hands on the answer, at a cost of one call. With
tools — actions dropped inside it — it may use them, in a loop, until it has an
answer, at a cost nobody can know in advance, which is why budgets exist.

It is an action itself. That is not a technicality: it means an AI step can be a
step anywhere an action can, and can itself be a tool of another AI step, with
the same nesting limit that bounds sub-workflows.

The loop is the engine's re-entrant ``WAITING`` node, the same one a split and a
loop use. One execution is one turn: ask the model, and either dispatch the
tools it asked for and suspend, or finish. Because the suspension is the
engine's own, everything the engine already does applies — the run survives a
worker dying, each tool call is inspectable, and a person can stand between the
model and anything irreversible.
"""

from __future__ import annotations

import datetime
import json

from django import forms
from django.conf import settings
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from ..engine import ActionPause
from ..instances import COMPLETED, FAILED, WAITING, AutomationAction
from ..models import BaseActionPluginModel
from ..tool_mixin import called_as_tool
from ..tools import ToolResult
from ..utilities.templates import validate_template
from ..widgets import SchemaWidget
from . import llm
from .budget import AgentBudget, BudgetExceeded
from .state import AgentState

#: Give up after this many rate-limit pauses.
MAX_LLM_RETRIES = 5

#: How deep AI steps and sub-workflows may nest before it is called a mistake.
#: Each level is a legitimate run and nothing about any one of them is wrong;
#: only the depth is. Without a limit a single prompt can start an unbounded
#: number of runs, which is the sort of thing one notices from the invoice.
#:
#: Dormant at present. Depth counts *runs*, and nothing starts a run from
#: inside another since the automation-calling tool was removed — an AI step
#: inside an AI step is a child action in one run, which the plugin tree bounds
#: on its own because a plugin cannot contain itself. The limit and the
#: ``parent_action`` chain it walks are kept for the sub-workflow action, which
#: is what will make runs nest again.
MAX_NESTING_DEPTH = 3


def _validate_json_schema(value):
    if not value:
        return
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise forms.ValidationError(_("Invalid JSON: %(error)s") % {"error": exc}) from exc
    if not isinstance(parsed, dict):
        raise forms.ValidationError(_("The schema must be a JSON object."))
    if parsed.get("type") == "object" and parsed.get("additionalProperties") is not False:
        raise forms.ValidationError(_('Object schemas must set "additionalProperties": false.'))


class AIStepForm(forms.Form):
    """What an editor fills in. Budgets only matter once there are tools."""

    model = forms.ChoiceField(
        label=_("Model"),
        choices=lambda: [(name, name) for name in llm.get_allowed_llm_models()],
        help_text=_("One of the models this project allows."),
    )
    system_prompt = forms.CharField(
        label=_("Instructions"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        validators=[validate_template],
        help_text=_("Standing context. Supports {{ dotted.path }} substitution."),
    )
    prompt = forms.CharField(
        label=_("Task"),
        widget=forms.Textarea(attrs={"rows": 6}),
        validators=[validate_template],
        help_text=_("What to do this run. Supports {{ dotted.path }} substitution."),
    )
    output_schema = forms.JSONField(
        label=_("Output shape"),
        required=False,
        widget=SchemaWidget(),
        validators=[_validate_json_schema],
        error_messages={"invalid": _("Invalid JSON.")},
        help_text=_(
            "Describe the fields the answer must contain. Field descriptions are read by the model and shape "
            "what it puts there. The answer becomes the new data rows."
        ),
    )
    max_turns = forms.IntegerField(
        required=False, label=_("Maximum turns"), initial=AgentBudget.max_turns, min_value=1
    )
    max_tool_calls = forms.IntegerField(
        required=False, label=_("Maximum tool calls"), initial=AgentBudget.max_tool_calls, min_value=1
    )
    max_tokens = forms.IntegerField(
        required=False, label=_("Maximum tokens"), initial=AgentBudget.max_tokens, min_value=1
    )
    deadline_seconds = forms.IntegerField(
        required=False, label=_("Deadline (seconds)"), initial=AgentBudget.deadline_seconds, min_value=1
    )
    llm_timeout = forms.IntegerField(
        required=False,
        label=_("Provider timeout (seconds)"),
        initial=120,
        min_value=1,
        help_text=_("A hung provider call holds its worker and its lease until something gives up."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        default = getattr(settings, "AUTOMATION_LLM_DEFAULT", None)
        if default:
            self.fields["model"].initial = default


class AIStepPluginModel(BaseActionPluginModel):
    """A model, a task, and whatever actions were put inside it."""

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    no_name = _("Two tools share the name %(name)s. Each needs a name of its own.")

    # -- the tools it may use ----------------------------------------------

    def _tools(self) -> list:
        """The actions inside this step, downcast, in editor order.

        Any action will do: what makes something a tool is being here, not
        being a special kind of thing.
        """
        tools = []
        for child in self.child_plugin_instances or []:
            if not hasattr(child, "config"):
                child, _unused = child.get_plugin_instance()
            if isinstance(child, BaseActionPluginModel):
                tools.append(child)
        return tools

    def messages(self) -> list[str]:
        seen, problems = set(), []
        for tool in self._tools():
            name = tool.effective_tool_name()
            if name in seen:
                problems.append(self.no_name % {"name": name})
            seen.add(name)
            problems.extend(tool.tool_messages())
        if self._tools() and (self.config or {}).get("output_schema"):
            problems.append(_("Output shape is ignored while this step has tools. Remove one or the other."))
        return problems

    def output_schema(self) -> dict | None:
        """The shape the answer is constrained to, if one was asked for."""
        raw = (self.config or {}).get("output_schema")
        if not raw:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw

    def budget(self) -> AgentBudget:
        config = self.config or {}
        return AgentBudget(
            max_turns=int(config.get("max_turns") or AgentBudget.max_turns),
            max_tool_calls=int(config.get("max_tool_calls") or AgentBudget.max_tool_calls),
            max_tokens=int(config.get("max_tokens") or AgentBudget.max_tokens),
            deadline_seconds=int(config.get("deadline_seconds") or AgentBudget.deadline_seconds),
        )

    # -- execution ---------------------------------------------------------

    def depth(self, action) -> int:
        """How many AI steps and sub-workflows deep this one already is."""
        depth = 0
        instance = action.automation_instance
        seen = set()
        while instance is not None and instance.pk not in seen:
            seen.add(instance.pk)
            parent = instance.parent_action
            if parent is None:
                break
            depth += 1
            instance = parent.automation_instance
        return depth

    def do_work(self, action, data, single_step=False, plugin_dict=None):
        """Take one turn."""
        from ..utilities.templates import safe_render

        config = self.config or {}
        tools = self._tools()

        # An AI step is an action, so it can be a tool of another AI step. Each
        # level is legitimate; only the depth is the mistake, and nothing in
        # the engine can see that it is a loop.
        if called_as_tool(action) and self.depth(action) >= MAX_NESTING_DEPTH:
            return FAILED, {"error": f"AI steps are nested more than {MAX_NESTING_DEPTH} deep."}
        state = AgentState.load(action)
        children = action.children.filter(replays__isnull=True)

        if children.filter(state=FAILED).exists():
            return FAILED, {"error": "An agent tool call failed"}
        if children.filter(finished__isnull=True).exists():
            return WAITING, {"turn": state.turn}

        budget = self.budget()
        if state.turn == 0:
            rows = data or []
            row = rows[0] if rows and isinstance(rows[0], dict) else {}
            context = {**row, "data": rows}
            state.started_at = now().isoformat()
            state.start(
                system=str(safe_render(str(config.get("system_prompt") or ""), context)) or None,
                prompt=str(safe_render(str(config.get("prompt") or ""), context)),
            )
        else:
            for child in children.order_by("created"):
                observation = (child.scratch or {}).get("tool_result")
                if observation and observation["call_id"] not in _answered(state):
                    result = ToolResult(
                        call_id=observation["call_id"],
                        content=observation["content"],
                        is_error=observation.get("is_error", False),
                    ).truncate(budget.max_observation_chars)
                    state.record_observation(result.call_id, str(result.content), result.is_error)

        by_name = {tool.effective_tool_name(): tool for tool in tools}
        wire = [tool.get_tool_spec().to_wire() for tool in tools]

        # A model naming a tool that does not exist is told what it does have
        # and asked again, here, with the budget checked each time round.
        while True:
            try:
                budget.check(state)
            except BudgetExceeded as exc:
                return self._out_of_budget(action, state, exc)

            try:
                reply = llm.complete(
                    model=str(config.get("model") or ""),
                    messages=state.messages,
                    tools=wire or None,
                    # A shape is asked for only when there is nothing else to
                    # ask for. Constraining the answer *and* offering tools on
                    # the same turn is provider-specific and untested here, so
                    # a step with tools ignores the shape and says so in the
                    # editor rather than behaving differently per provider.
                    schema=self.output_schema() if not tools else None,
                    timeout=int(config.get("llm_timeout") or 120),
                )
            except llm.LLMRateLimited as exc:
                # A pause, not a failure: the engine reschedules and the
                # conversation is kept so the turn resumes where it stopped.
                state.save(action)
                retries = int((action.scratch or {}).get("llm_retries", 0))
                if retries + 1 >= MAX_LLM_RETRIES:
                    return FAILED, {"error": f"Rate limited {MAX_LLM_RETRIES} times, giving up: {exc}"}
                AutomationAction.objects.filter(pk=action.pk).update(
                    scratch={**(action.scratch or {}), "llm_retries": retries + 1}
                )
                raise ActionPause(
                    until=now() + datetime.timedelta(seconds=exc.retry_after),
                    message=f"LLM rate limited, retry {retries + 1}/{MAX_LLM_RETRIES}",
                ) from exc
            except llm.LLMError as exc:
                state.save(action)
                return FAILED, {"error": str(exc)}

            state.record_reply(reply)

            # Checked before anything is done with the reply, and whether or
            # not it asked for tools: truncation is worse for a tool call than
            # for text, because arguments cut short still parse — into
            # something plausible and wrong, which a tool then runs.
            if reply.incomplete:
                state.save(action)
                return FAILED, {
                    "error": f"The model's reply is incomplete: {reply.incomplete}.",
                    "finish_reason": reply.finish_reason,
                    "turns": state.turn,
                }

            # Every call the model asked for is answered, whether or not it
            # runs. A provider rejects a conversation whose assistant turn
            # requests a tool that nothing replies to, so an unanswered call
            # poisons the next request rather than simplifying it.
            wanted = [call for call in reply.tool_calls if call.name in by_name]
            unknown = [call for call in reply.tool_calls if call.name not in by_name]
            for call in unknown:
                state.record_observation(
                    call.id,
                    f"No tool named {call.name!r}. Available: {', '.join(sorted(by_name))}.",
                    is_error=True,
                )

            # The tool-call budget is spent before the next turn, not at it.
            try:
                allowed = budget.allow(state, wanted)
            except BudgetExceeded as exc:
                return self._out_of_budget(action, state, exc)
            for call in wanted[len(allowed) :]:
                state.record_observation(call.id, "Not run: this run's tool-call budget is spent.", is_error=True)
            wanted = allowed

            if unknown and not wanted:
                # Nothing to dispatch, so suspending would leave this node
                # waiting on a child that never arrives. It is asked again
                # here, against the same budget, so a model that keeps
                # guessing still stops.
                continue
            break

        if wanted:
            state.queue(wanted)
            state.save(action)
            return WAITING, {"turn": state.turn}

        state.save(action)
        if reply.json is not None:
            rows = reply.json if isinstance(reply.json, list) else [reply.json]
            return COMPLETED, [row if isinstance(row, dict) else {"value": row} for row in rows]
        return COMPLETED, [{"text": reply.text, "turns": state.turn, "usage": state.usage}]

    def _out_of_budget(self, action, state, exc):
        """Stop the run, keeping the conversation that explains why."""
        state.save(action)
        return FAILED, {"error": str(exc), "turns": state.turn, "tool_calls": state.tool_calls}

    def get_next_actions(self, action):
        """Dispatch the tool calls this turn asked for.

        Gated on what has already been dispatched rather than on there being no
        children, because an agent spawns on every turn — and because a woken
        agent must not run a tool call it already ran.
        """
        if action.state != WAITING:
            return super().get_next_actions(action)

        children = action.children.filter(replays__isnull=True)
        if children.filter(finished__isnull=True).exists():
            return []

        state = AgentState.load(action)
        pending = state.undispatched()
        if not pending:
            return []

        by_name = {tool.effective_tool_name(): tool for tool in self._tools()}
        created = []
        for call in pending:
            tool = by_name.get(call.name)
            if tool is None:
                continue
            created.append(
                AutomationAction.objects.create(
                    previous=action,
                    parent=action,
                    automation_instance=action.automation_instance,
                    plugin_ptr=tool.uuid,
                    scratch={
                        "tool_call": {
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                            "malformed": call.malformed,
                        }
                    },
                    finished=None,
                )
            )
        state.mark_dispatched(pending)
        state.save(action)
        return created


def _answered(state) -> set:
    """Call ids the conversation already carries an answer for."""
    return {
        message.get("tool_call_id")
        for message in state.messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }

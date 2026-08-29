"""The Agent and Tool nodes.

An agent is the re-entrant ``WAITING`` node the engine already knows how to run,
with a different termination rule: a split fans out once and joins, a loop goes
round while a condition holds, an agent goes round until a model stops asking
for tools. Each tool call is therefore a first-class ``AutomationAction`` —
recorded, retryable, and interruptible by a human before it runs.

These models live in the ``ai`` app rather than the core one, so a project that
never calls a model does not carry their tables.
"""

from __future__ import annotations

from django.db import models
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from ..engine import ActionPause
from ..instances import COMPLETED, FAILED, WAITING, AutomationAction
from ..models import AutomationPluginModel
from . import llm
from .budget import AgentBudget, BudgetExceeded
from .state import AgentState
from .tools import ToolResult, ToolSpec, ToolValidationError, schema_from_form, validate_arguments

#: Actions whose effects cannot be taken back. Exposed as tools, they require a
#: human by default: the conservative default can be relaxed by whoever adds the
#: tool, and the opposite mistake cannot be undone.
DESTRUCTIVE_PLUGINS = {"CreateModelAction", "UpdateModelAction", "MailAction"}


class AgentToolPluginModel(AutomationPluginModel):
    """Offers its single child action to the parent agent as a tool.

    The wrapped action is an ordinary action, configured in the editor as
    usual. This node adds only what a model needs to know — a name, a
    description — and, importantly, which of the action's inputs the model may
    fill. Everything else stays bound to the editor's expressions and is never
    shown to the model.
    """

    tool_name = models.SlugField(
        max_length=64,
        verbose_name=_("Tool name"),
        help_text=_("What the model calls this tool. Letters, digits, underscore and hyphen."),
    )
    tool_description = models.TextField(
        verbose_name=_("Description"),
        help_text=_(
            "What this tool does and when to use it. This is the only thing telling the model "
            "when to reach for it, and has more influence on whether an agent works than any "
            "other single setting."
        ),
    )
    exposed_fields = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Inputs the model may fill"),
        help_text=_(
            "Field names from the wrapped action. Everything omitted stays bound to the "
            "expression you configured on the action and is never offered to the model."
        ),
    )
    requires_approval = models.BooleanField(
        default=None,
        null=True,
        blank=True,
        verbose_name=_("Requires approval"),
        help_text=_(
            "Pause for a person to approve each call before it runs. Left unset, an action "
            "whose effects cannot be taken back requires approval and everything else does not."
        ),
    )

    class Meta:
        verbose_name = _("Agent tool")
        verbose_name_plural = _("Agent tools")

    no_action = _("This tool wraps nothing. Add the action it should run inside it.")
    unknown_fields = _("Exposed inputs not found on the action: %(names)s")

    # -- the wrapped action ------------------------------------------------

    def _action_plugin(self):
        """The action this tool runs, downcast, or ``None``."""
        for child in self.child_plugin_instances or []:
            if not hasattr(child, "uuid"):
                child, _unused = child.get_plugin_instance()
            return child
        return None

    def _data_form(self):
        """The wrapped action's declared inputs, which are also its schema."""
        from cms.plugin_pool import plugin_pool

        plugin = self._action_plugin()
        if plugin is None:
            return None
        try:
            return getattr(plugin_pool.get_plugin(plugin.plugin_type), "data_form", None)
        except KeyError:
            return None

    def messages(self) -> list[str]:
        """Editor warnings for a tool that cannot work as configured."""
        if self._action_plugin() is None:
            return [self.no_action]
        form = self._data_form()
        if form is None:
            return []
        unknown = [name for name in (self.exposed_fields or []) if name not in form.base_fields]
        if unknown:
            return [self.unknown_fields % {"names": ", ".join(unknown)}]
        return []

    def is_destructive(self) -> bool:
        plugin = self._action_plugin()
        return bool(plugin and plugin.plugin_type in DESTRUCTIVE_PLUGINS)

    def needs_approval(self) -> bool:
        """Whether a person has to see this call before it runs.

        Resolved when the question is asked, not when the tool is saved. An
        editor adds the tool and *then* drops an action into it, so at save
        time the tool wraps nothing and any decision made there is a decision
        about emptiness. Deciding late also means moving a different action
        into an existing tool cannot silently drop the gate.
        """
        if self.requires_approval is None:
            return self.is_destructive()
        return self.requires_approval

    # -- the contract ------------------------------------------------------

    def get_tool_spec(self) -> ToolSpec:
        """Describe this tool to the model."""
        form = self._data_form()
        exposed = list(self.exposed_fields or [])
        parameters = (
            schema_from_form(form, include=exposed)
            if form is not None
            else {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        )
        return ToolSpec(
            name=self.tool_name,
            description=self.tool_description,
            parameters=parameters,
            requires_approval=self.needs_approval(),
            destructive=self.is_destructive(),
        )

    def run(self, call, action: AutomationAction, rows: list) -> tuple[str, ToolResult | None, list]:
        """Validate the model's arguments and run the wrapped action.

        The action is run in this node's own execution rather than scheduled as
        a further step, which is what keeps one tool call to one execution
        record — the thing that makes an agent's history readable.

        It is run through :meth:`execute`, not :meth:`perform`. ``perform`` is
        where most actions put their work, but not all of them: *Wait for User*
        pauses from ``execute`` and leaves ``perform`` as the base pass-through,
        so calling ``perform`` would make an escalation tool report success
        without anyone having seen it. Going through ``execute`` also means a
        wrapped action that wants to wait can, and this node waits with it.

        :returns: The state this tool call reached, the result to report to the
            model (``None`` while waiting for a person), and the output — rows
            for a finished call, the wrapped action's own output while it waits.
        """
        plugin = self._action_plugin()
        if plugin is None:
            return COMPLETED, ToolResult(call_id=call.id, content="This tool has no action to run.", is_error=True), []

        form = self._data_form()
        exposed = list(self.exposed_fields or [])
        try:
            arguments = validate_arguments(form, call.arguments, allowed=exposed) if form else {}
        except ToolValidationError as exc:
            # Handed back for the model to correct rather than failing the run.
            return COMPLETED, ToolResult(call_id=call.id, content=str(exc), is_error=True), []

        # An action reads its inputs in one of two ways, so the model's values
        # are put where each kind will look.
        #
        # Actions whose editor inputs are expressions over the automation's data
        # go through ``resolve_inputs``, and take ``_input_overrides``: the
        # override bypasses resolution, because a value the model supplied is
        # already the literal and resolving "Ship it" would read it as a data
        # path.
        #
        # Actions configured with literal values — the model actions, Wait for
        # User, LLM Prompt — read ``config`` directly and never see an override.
        # For those, the model's arguments *are* config: ``validate_arguments``
        # put them through the same form field the editor fills in, so what the
        # action receives is indistinguishable from someone having typed it.
        #
        # Both are set, because either kind may be wrapped and this node cannot
        # usefully tell them apart. The config is put back afterwards: the
        # plugin instance comes from a map that outlives this call, and one tool
        # call must not leave its arguments behind for the next.
        plugin._input_overrides = arguments
        configured = plugin.config
        if arguments:
            plugin.config = {**(configured or {}), **arguments}
        try:
            state, output = plugin.execute(action, rows)
        except ActionPause:
            # The engine's own pause signal — a rate limit, a backoff. It is
            # not an observation for the model; it means "run me again later",
            # and only the engine can do that.
            raise
        except Exception as exc:  # noqa: BLE001 — a failing tool is an observation
            return COMPLETED, ToolResult(call_id=call.id, content=f"{type(exc).__name__}: {exc}", is_error=True), []
        finally:
            plugin.config = configured
            plugin._input_overrides = None

        if state == WAITING:
            # The wrapped action wants a person. It has set whatever it needs on
            # this action to say so; this node simply waits with it, and reports
            # to the model once that person has answered. Its output is passed
            # back untouched, because it is what that person is meant to read.
            return WAITING, None, output
        rows_out = output if isinstance(output, list) else [{"value": output}]
        if state == FAILED:
            return COMPLETED, ToolResult(call_id=call.id, content=str(output), is_error=True), []
        return COMPLETED, ToolResult(call_id=call.id, content=str(rows_out), rows=rows_out), rows_out

    # -- execution ---------------------------------------------------------

    #: A tool call waiting for approval has not run yet, so resuming it means
    #: "go ahead" rather than "you are done".
    resume_reenters = True

    def scratch_for_replay(self, scratch: dict) -> dict:
        """The call itself, and nothing else.

        A replayed tool call has to be the same call — the model asked for a
        particular tool with particular arguments, and the transcript is waiting
        for an answer carrying that id. Everything else is deliberately dropped.
        An approval was granted to a call that then failed; replaying is an
        operator running it again, and that decision is theirs to have someone
        confirm, not the failed row's to grant in advance.
        """
        call = scratch.get("tool_call")
        return {"tool_call": call} if call else {}

    def get_next_actions(self, action) -> list:
        """Nothing follows a tool call except its agent.

        The default walks the plugin tree and starts whatever comes next, which
        for a tool is the sibling tool — an agent's second tool would run
        because its first one did, with no call behind it and no model having
        asked. Tools are dispatched by their agent or not at all; a finished one
        just wakes it.
        """
        return []

    def on_resume(self, action, user, data) -> None:
        """Write down the person's decision before this node runs again.

        Consent cannot be inferred from the node's own state: a worker that
        crashed between pausing and being recovered leaves a row
        indistinguishable from an approved one, and the whole point of the gate
        is that the difference matters. So it is recorded, in the transaction
        that resumes.
        """
        scratch = dict(action.scratch or {})
        if scratch.get("awaiting_approval"):
            scratch["approved"] = True
            scratch["approved_by"] = getattr(user, "pk", None)
        elif scratch.get("awaiting_input"):
            scratch["input"] = data or {}
        AutomationAction.objects.filter(pk=action.pk).update(scratch=scratch)
        action.scratch = scratch

    def execute(self, action, data, single_step=False, plugin_dict=None):
        """Run one tool call, pausing first if it needs a person.

        A call passes through up to three of this node's executions: pause for
        approval, run, and — if the wrapped action itself waits for someone —
        report what that person said. Each pause is the engine's ordinary
        human-in-the-loop wait, so an agent's tool call appears in the same
        *Open tasks* list as any other waiting step, with the arguments the
        model chose visible on it.
        """
        scratch = dict(action.scratch or {})
        raw = scratch.get("tool_call") or {}
        call = llm.ToolCall(id=raw.get("id", ""), name=raw.get("name", ""), arguments=raw.get("arguments") or {})

        # The wrapped action was waiting for a person, and that person has
        # answered. What they said is what the model hears back.
        if scratch.get("awaiting_input"):
            answer = scratch.get("input") or {}
            rows = data or []
            self._record(action, scratch, ToolResult(call_id=call.id, content=str(answer or rows)))
            return COMPLETED, rows

        if self.needs_approval() and not scratch.get("approved"):
            action.requires_interaction = True
            scratch["awaiting_approval"] = True
            AutomationAction.objects.filter(pk=action.pk).update(scratch=scratch)
            action.scratch = scratch
            return WAITING, self._for_the_person(call)

        state, result, output = self.run(call, action, data or [])
        if state == WAITING:
            # The wrapped action wants a person of its own. It has already set
            # what it needs on this action to say so; this node waits with it,
            # keeping whatever the action wrote for that person to read.
            scratch["awaiting_input"] = True
            scratch.pop("awaiting_approval", None)
            AutomationAction.objects.filter(pk=action.pk).update(scratch=scratch)
            action.scratch = scratch
            waiting = output if isinstance(output, dict) else {}
            return WAITING, {**waiting, **self._for_the_person(call)}

        self._record(action, scratch, result)
        return COMPLETED, output

    def _for_the_person(self, call) -> dict:
        """What the *Open tasks* page needs in order to be worth reading.

        Someone approving a call is being asked to make a decision, and cannot
        make it from the fact that a decision is due. They need to know which
        tool wants to run, with what, and whether it can be taken back.
        """
        return {
            "tool": call.name,
            "arguments": call.arguments,
            "destructive": self.is_destructive(),
        }

    def _record(self, action, scratch, result: ToolResult) -> None:
        """Leave the observation where the agent will look for it."""
        scratch = {
            **scratch,
            "tool_result": {"call_id": result.call_id, "content": result.content, "is_error": result.is_error},
        }
        scratch.pop("awaiting_input", None)
        scratch.pop("awaiting_approval", None)
        AutomationAction.objects.filter(pk=action.pk).update(scratch=scratch)
        action.scratch = scratch


class AgentPluginModel(AutomationPluginModel):
    """An LLM that chooses and calls the tools declared inside it.

    One engine execution is one turn: ask the model, and either dispatch the
    tools it asked for and suspend, or finish. Because the suspension is the
    engine's own ``WAITING``, everything the engine already does applies — the
    run survives a worker dying, each tool call is inspectable, and a human can
    stand between the model and anything irreversible.
    """

    question = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Description"),
        help_text=_("What this agent is for, shown in the editor."),
    )
    model = models.CharField(
        max_length=255,
        verbose_name=_("Model"),
        help_text=_("A model string from AUTOMATION_LLM_MODELS, e.g. anthropic/claude-opus-4-8."),
    )
    system_prompt = models.TextField(
        blank=True,
        verbose_name=_("System prompt"),
        help_text=_("Standing instructions. Supports {{ dotted.path }} against the automation data."),
    )
    prompt = models.TextField(
        verbose_name=_("Task"),
        help_text=_("What to do this run. Supports {{ dotted.path }} against the automation data."),
    )

    max_turns = models.PositiveIntegerField(default=AgentBudget.max_turns, verbose_name=_("Maximum turns"))
    max_tool_calls = models.PositiveIntegerField(
        default=AgentBudget.max_tool_calls, verbose_name=_("Maximum tool calls")
    )
    max_tokens = models.PositiveIntegerField(default=AgentBudget.max_tokens, verbose_name=_("Maximum tokens"))
    deadline_seconds = models.PositiveIntegerField(
        default=AgentBudget.deadline_seconds, verbose_name=_("Deadline (seconds)")
    )
    llm_timeout = models.PositiveIntegerField(
        default=120,
        verbose_name=_("Provider timeout (seconds)"),
        help_text=_("A hung provider call holds its worker and its lease until something gives up."),
    )

    class Meta:
        verbose_name = _("Agent")
        verbose_name_plural = _("Agents")

    no_tools = _("This agent has no tools. Add the actions it may call inside it.")
    duplicate_names = _("Two tools share the name %(name)s. Each tool needs a name of its own.")

    # -- tools -------------------------------------------------------------

    def _tools(self) -> list[AgentToolPluginModel]:
        """The tool nodes declared inside this agent, in editor order."""
        tools = []
        for child in self.child_plugin_instances or []:
            if child.plugin_type != "AutomationAgentTool":
                continue
            if not hasattr(child, "tool_name"):
                child, _unused = child.get_plugin_instance()
            tools.append(child)
        return tools

    def messages(self) -> list[str]:
        tools = self._tools()
        if not tools:
            return [self.no_tools]
        seen, problems = set(), []
        for tool in tools:
            if tool.tool_name in seen:
                problems.append(self.duplicate_names % {"name": tool.tool_name})
            seen.add(tool.tool_name)
        return problems

    def budget(self) -> AgentBudget:
        return AgentBudget(
            max_turns=self.max_turns,
            max_tool_calls=self.max_tool_calls,
            max_tokens=self.max_tokens,
            deadline_seconds=self.deadline_seconds,
        )

    # -- execution ---------------------------------------------------------

    def execute(self, action, data, single_step=False, plugin_dict=None):
        """Take one turn."""
        from ..utilities.templates import safe_render

        tools = self._tools()
        if not tools:
            return COMPLETED, data

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
                system=str(safe_render(self.system_prompt, context)) or None,
                prompt=str(safe_render(self.prompt, context)),
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

        by_name = {tool.tool_name: tool for tool in tools}
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
                    model=self.model,
                    messages=state.messages,
                    tools=wire,
                    timeout=self.llm_timeout,
                )
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

        by_name = {tool.tool_name: tool for tool in self._tools()}
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
                    scratch={"tool_call": {"id": call.id, "name": call.name, "arguments": call.arguments}},
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

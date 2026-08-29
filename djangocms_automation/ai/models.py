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
        default=False,
        verbose_name=_("Requires approval"),
        help_text=_("Pause for a person to approve each call before it runs."),
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
            requires_approval=self.requires_approval,
            destructive=self.is_destructive(),
        )

    def run(self, call, action: AutomationAction, rows: list) -> ToolResult:
        """Validate the model's arguments and run the wrapped action.

        The action is called directly rather than scheduled as its own step.
        That keeps one tool call to one execution record, which is what makes an
        agent's history readable; an action needing several steps of its own is
        a workflow, and belongs behind a sub-workflow rather than a tool.
        """
        plugin = self._action_plugin()
        if plugin is None:
            return ToolResult(call_id=call.id, content="This tool has no action to run.", is_error=True)

        form = self._data_form()
        exposed = list(self.exposed_fields or [])
        try:
            arguments = validate_arguments(form, call.arguments, allowed=exposed) if form else {}
        except ToolValidationError as exc:
            # Handed back for the model to correct rather than failing the run.
            return ToolResult(call_id=call.id, content=str(exc), is_error=True)

        # The action resolves its own inputs, so the model's values are handed
        # to it on the instance rather than as an argument. No action needs to
        # know it is being used as a tool.
        plugin._input_overrides = arguments
        try:
            output = plugin.perform(action, rows)
        except Exception as exc:  # noqa: BLE001 — a failing tool is an observation
            return ToolResult(call_id=call.id, content=f"{type(exc).__name__}: {exc}", is_error=True)

        rows_out = output if isinstance(output, list) else [{"value": output}]
        return ToolResult(call_id=call.id, content=str(rows_out), rows=rows_out)

    # -- execution ---------------------------------------------------------

    def execute(self, action, data, single_step=False, plugin_dict=None):
        """Run one tool call, pausing first if it needs a person.

        The approval gate is the existing human-in-the-loop pause, so an agent's
        tool call appears in the same *Open tasks* list as any other waiting
        step, with the arguments the model chose visible on it.
        """
        scratch = action.scratch if isinstance(action.scratch, dict) else {}
        raw = scratch.get("tool_call") or {}
        call = llm.ToolCall(id=raw.get("id", ""), name=raw.get("name", ""), arguments=raw.get("arguments") or {})

        if self.requires_approval and not scratch.get("approved"):
            action.requires_interaction = True
            AutomationAction.objects.filter(pk=action.pk).update(scratch={**scratch, "awaiting_approval": True})
            return WAITING, {"tool": call.name, "arguments": call.arguments}

        result = self.run(call, action, data or [])
        AutomationAction.objects.filter(pk=action.pk).update(
            scratch={
                **scratch,
                "tool_result": {"call_id": call.id, "content": result.content, "is_error": result.is_error},
            }
        )
        return COMPLETED, result.rows


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

        # A model naming a tool that does not exist has asked for nothing this
        # node can dispatch, so suspending would leave the agent waiting on a
        # child that never arrives. It is told what it does have and asked
        # again, here, with the budget checked each time round so a model that
        # keeps guessing still stops.
        while True:
            try:
                budget.check(state)
            except BudgetExceeded as exc:
                state.save(action)
                return FAILED, {"error": str(exc), "turns": state.turn, "tool_calls": state.tool_calls}

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

            # One call per turn: easier to reason about, to approve, and to read
            # back afterwards. The engine could dispatch several — the join
            # logic already supports it — but nothing yet needs it.
            wanted = [call for call in reply.tool_calls if call.name in by_name][:1]
            unknown = [call for call in reply.tool_calls if call.name not in by_name]
            if unknown and not wanted:
                state.record_observation(
                    unknown[0].id,
                    f"No tool named {unknown[0].name!r}. Available: {', '.join(sorted(by_name))}.",
                    is_error=True,
                )
                continue
            break

        if wanted:
            state.queue(wanted)
            state.save(action)
            return WAITING, {"turn": state.turn}

        state.save(action)
        return COMPLETED, [{"text": reply.text, "turns": state.turn, "usage": state.usage}]

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

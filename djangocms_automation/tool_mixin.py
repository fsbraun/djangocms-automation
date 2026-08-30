"""What makes an action usable as a tool.

A tool is an action with wiring. The action is the capability — code, written by
a developer, that does something. The wiring is an editor's decision about this
particular use of it: what the model may fill in, what stays bound to the
automation's data, what it is called, and whether a person has to see the call
first.

Because it is the same object, there is no wrapper node and no second kind of
thing to build. An action dropped inside an AI step *is* a tool; the same action
anywhere else is an ordinary step. Everything here branches on that one
question — is this execution a tool call? — and does nothing at all when it is
not.
"""

from __future__ import annotations

import logging

from django.utils.translation import gettext_lazy as _

from .instances import COMPLETED, FAILED, WAITING, AutomationAction
from .tools import (
    TOOL_NAME_RE,
    ToolCall,
    ToolError,
    ToolResult,
    ToolSpec,
    ToolValidationError,
    schema_from_form,
    validate_arguments,
)

__all__ = ["ToolMixin", "called_as_tool"]

logger = logging.getLogger(__name__)


def _as_tool_name(source: str) -> str:
    """Squeeze a human name into something every provider will accept."""
    import re
    import unicodedata

    # Transliterated first, so a name in another script yields something rather
    # than nothing: "Rückruf" becomes "ruckruf", not "rckruf".
    folded = unicodedata.normalize("NFKD", str(source or "")).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", folded).strip("_").lower()
    return cleaned[:64].rstrip("_")


def called_as_tool(action) -> bool:
    """Whether this execution is a model's tool call rather than a flow step.

    The call the AI step dispatched is written on the action, so the question
    answers itself from state rather than from the plugin tree — which also
    means a replayed call is still a call.
    """
    return bool((getattr(action, "scratch", None) or {}).get("tool_call"))


class ToolMixin:
    """The tool half of an action.

    Mixed into every action's model. Outside an AI step none of it runs: the
    wiring fields stay empty, ``execute`` delegates straight through, and the
    action behaves exactly as it did before tools existed.
    """

    #: Errors an editor sees when the wiring cannot work.
    no_name = _("This tool has no name. The model needs one to call it by.")

    # -- what the model is told --------------------------------------------

    def tool_inputs(self) -> list[str]:
        """The inputs the model may fill. Everything else stays bound."""
        return list(self.exposed_fields or [])

    def is_destructive(self) -> bool:
        """Whether a call cannot be taken back.

        Read from the action class, because the action's author is the only one
        who knows. It decides the automatic approval gate.
        """
        from cms.plugin_pool import plugin_pool

        try:
            return bool(getattr(plugin_pool.get_plugin(self.plugin_type), "destructive", False))
        except KeyError:
            return False

    def needs_approval(self) -> bool:
        """Whether a person has to see this call before it runs.

        Resolved when the question is asked rather than when the tool was
        saved, so it follows what the action actually is.
        """
        if self.requires_approval is None:
            return self.is_destructive()
        return self.requires_approval

    def effective_tool_name(self) -> str:
        """What the model calls this, defaulting to the action's own name.

        The default is *derived*, so it has to be made legal here rather than
        hoped about: an action's name is prose, and a translated one is prose
        in a language whose punctuation and script the tool-name rule does not
        admit. Left as it came, a perfectly ordinary action would build a spec
        that raises while assembling the provider request — a run failing at
        the last moment for a name nobody typed.
        """
        if self.tool_name:
            return self.tool_name
        from cms.plugin_pool import plugin_pool

        try:
            source = str(plugin_pool.get_plugin(self.plugin_type).name)
        except KeyError:
            source = self.plugin_type
        return _as_tool_name(source) or _as_tool_name(self.plugin_type) or "tool"

    def effective_tool_description(self) -> str:
        """When to use it, defaulting to whatever the action says of itself.

        A default is a poor description — an action describes its mechanism,
        not the occasion for using it — but a blank one in front of an editor
        who has not yet learned that descriptions matter is worse, because they
        will write the mechanism too and move on.
        """
        if self.tool_description:
            return self.tool_description
        from cms.plugin_pool import plugin_pool

        try:
            plugin = plugin_pool.get_plugin(self.plugin_type)
        except KeyError:
            return ""
        return str(getattr(plugin, "description", "") or plugin.name)

    def parameters_schema(self) -> dict:
        """The JSON schema of what this tool accepts."""
        form = self.tool_data_form()
        if form is None:
            return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        return schema_from_form(form, include=self.tool_inputs())

    def tool_data_form(self):
        """The action's declared inputs, which are also its schema."""
        from cms.plugin_pool import plugin_pool

        try:
            return getattr(plugin_pool.get_plugin(self.plugin_type), "data_form", None)
        except KeyError:
            return None

    def get_tool_spec(self) -> ToolSpec:
        """Describe this tool to the model."""
        return ToolSpec(
            name=self.effective_tool_name(),
            description=self.effective_tool_description(),
            parameters=self.parameters_schema(),
            requires_approval=self.needs_approval(),
            destructive=self.is_destructive(),
        )

    def tool_messages(self) -> list[str]:
        """Editor warnings about the wiring, as opposed to the action."""
        problems = []
        if self.tool_name and not TOOL_NAME_RE.match(self.tool_name):
            problems.append(
                _("'%(name)s' cannot be a tool name. Use letters, digits, underscore or hyphen, up to 64.")
                % {"name": self.tool_name}
            )
        form = self.tool_data_form()
        if form is not None:
            unknown = [name for name in self.tool_inputs() if name not in form.base_fields]
            if unknown:
                problems.append(_("Inputs not found on this action: %(names)s") % {"names": ", ".join(unknown)})
        return problems

    # -- the contract ------------------------------------------------------

    def validate_tool_arguments(self, call, rows: list | None = None) -> tuple[dict, ToolResult | None]:
        """Check what the model sent against the action's own form.

        Once per row, because an action runs once per row. The model's
        arguments are the same each time but the bound half is not — an
        expression resolves against the row in front of it — so checking only
        the first row lets every row after it through unexamined, and an action
        validating a relationship between the two halves would have its rule
        applied to one row out of a hundred.
        """
        form = self.tool_data_form()
        if form is None:
            return {}, None

        exposed = self.tool_inputs()
        mappings = frozenset(getattr(self, "expression_mappings", frozenset()))
        arguments = {}
        for row in self._rows_to_check(rows):
            try:
                arguments = validate_arguments(
                    form,
                    call.arguments,
                    allowed=exposed,
                    literal_mappings=mappings,
                    bound=self._bound_values_for(row, rows or []),
                )
            except ToolValidationError as exc:
                # Handed back for the model to correct rather than failing the
                # run. The first row that refuses is the one reported: a model
                # cannot act on a hundred variations of the same complaint.
                return {}, ToolResult(call_id=call.id, content=str(exc), is_error=True)
        return arguments, None

    def _rows_to_check(self, rows: list | None) -> list:
        """Every row the action will run against, or one empty one.

        Normalised the way the actions themselves normalise: a row that is not
        a mapping is run as ``{"value": row}``, so discarding it here would let
        it through unchecked while it still had effects.
        """
        actual = [row if isinstance(row, dict) else {"value": row} for row in (rows or [])]
        return actual or [{}]

    def _bound_values_for(self, row: dict, rows: list) -> dict:
        """What the editor configured, as it resolves against a single row."""
        from cms.plugin_pool import plugin_pool

        try:
            plugin = plugin_pool.get_plugin(self.plugin_type)
        except KeyError:
            return {}
        exposed = set(self.tool_inputs())
        configured = {name: value for name, value in (self.config or {}).items() if name not in exposed}
        if getattr(plugin, "convert_data_form", True) is False:
            return configured  # already literal values
        return self._bound_for_row(configured, row, rows)

    def _bound_values(self, rows: list | None = None) -> dict:
        """What the editor configured, as the values the action will actually use.

        Not the expressions but what they come to. They are knowable here — the
        automation's data is in hand, and resolving them is what the action is
        about to do anyway — so a cross-field ``clean`` sees the same pair of
        values the action will, rather than one half and a hole.

        An input whose expression will not resolve is left out rather than
        guessed at: that is the action's own problem to report when it runs,
        not a reason to refuse the model's arguments.
        """
        from cms.plugin_pool import plugin_pool

        try:
            plugin = plugin_pool.get_plugin(self.plugin_type)
        except KeyError:
            return {}
        exposed = set(self.tool_inputs())
        configured = {name: value for name, value in (self.config or {}).items() if name not in exposed}
        if getattr(plugin, "convert_data_form", True) is False:
            return configured  # already literal values

        rows = rows or []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        return self._bound_for_row(configured, row, rows)

    def _bound_for_row(self, configured: dict, row: dict, rows: list) -> dict:
        """The configured inputs as they resolve against one row."""
        try:
            resolved = self.resolve_inputs(row, rows)
        except Exception:  # noqa: BLE001 — an expression that will not resolve
            return {}
        return {name: resolved[name] for name in configured if name in resolved}

    def validate_call(self, call, rows: list | None = None) -> tuple[dict, ToolResult | None]:
        """Check a call before anything is done about it.

        Before the approval gate, not after. Approval is for calls that could
        run: a call that cannot spends a person's attention on a decision with
        no outcome, and holds back the error the model needs in order to
        correct itself until somebody has been found to look at it.
        """
        if call.malformed:
            # Not run under any circumstances. A tool whose inputs are all
            # optional would otherwise accept the empty set this parsed to and
            # do something wholly unasked for — an unfiltered query, say.
            return {}, ToolResult(
                call_id=call.id,
                content="Your arguments were not valid JSON. Send the call again with a JSON object.",
                is_error=True,
            )
        return self.validate_tool_arguments(call, rows)

    # -- execution ---------------------------------------------------------

    def resume_reenters(self, action) -> bool:
        """A tool call waiting for approval has not run yet.

        So resuming it means "go ahead" rather than "you are done" — but only
        when it *is* a tool call. The same action drawn into a flow is finished
        when somebody resumes it, as it always was.
        """
        return called_as_tool(action)

    def scratch_for_replay(self, scratch: dict) -> dict:
        """The call itself, and nothing else.

        A replayed tool call has to be the same call — the model asked for a
        particular tool with particular arguments, and the transcript is
        waiting for an answer carrying that id. Everything else is deliberately
        dropped. An approval was granted to a call that then failed; replaying
        is an operator running it again, and that decision is theirs to have
        someone confirm, not the failed row's to grant in advance.
        """
        call = scratch.get("tool_call")
        return {"tool_call": call} if call else {}

    def get_next_actions(self, action) -> list:
        """Nothing follows a tool call except the step that asked for it.

        The default walks the plugin tree and starts whatever comes next, which
        inside an AI step is the tool beside this one — so a second tool would
        run because the first did, with no call behind it and no model having
        asked. A finished call just wakes the step.
        """
        if called_as_tool(action):
            return []
        return super().get_next_actions(action)

    def on_resume(self, action, user, data) -> None:
        """Write down the person's decision before this node runs again.

        Consent cannot be inferred from the node's own state: a worker that
        crashed between pausing and being recovered leaves a row
        indistinguishable from an approved one, and the whole point of the gate
        is that the difference matters.
        """
        if not called_as_tool(action):
            return super().on_resume(action, user, data)
        scratch = dict(action.scratch or {})
        if scratch.get("awaiting_approval"):
            scratch["approved"] = True
            scratch["approved_by"] = getattr(user, "pk", None)
        elif scratch.get("awaiting_input"):
            scratch["input"] = data or {}
        AutomationAction.objects.filter(pk=action.pk).update(scratch=scratch)
        action.scratch = scratch

    def execute(self, action, data, single_step=False, plugin_dict=None):
        """Run this action — as a step, or as one call from a model.

        As a tool, a call passes through up to three of these executions: pause
        for approval, run, and — if the action itself waits for somebody —
        report what came back. Each pause is the engine's ordinary
        human-in-the-loop wait, so a tool call appears in the same *Open tasks*
        list as any other waiting step.
        """
        if not called_as_tool(action):
            return self.do_work(action, data, single_step=single_step, plugin_dict=plugin_dict)

        scratch = dict(action.scratch or {})
        raw = scratch.get("tool_call") or {}
        call = ToolCall(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            arguments=raw.get("arguments") or {},
            malformed=bool(raw.get("malformed")),
        )

        # Waiting for something, and it has arrived.
        if scratch.get("awaiting_input"):
            rows = data or []
            answer = scratch.get("input") or {}
            self._record_observation(action, ToolResult(call_id=call.id, content=str(answer or rows), rows=rows))
            return COMPLETED, rows

        arguments, refusal = self.validate_call(call, data or [])
        if refusal is not None:
            self._record_observation(action, refusal)
            return COMPLETED, []

        if self.needs_approval() and not scratch.get("approved"):
            action.requires_interaction = True
            scratch["awaiting_approval"] = True
            AutomationAction.objects.filter(pk=action.pk).update(scratch=scratch)
            action.scratch = scratch
            return WAITING, self._for_the_person(call)

        state, result, output = self.run_tool_call(call, action, data or [], arguments)
        if state == WAITING:
            # Read back rather than reused: what just ran may have written its
            # own state here — a nested AI step saves its conversation and the
            # calls it is waiting on — and the snapshot taken before it ran no
            # longer describes this row.
            scratch = dict(action.scratch or {})
            scratch.pop("awaiting_approval", None)
            if action.requires_interaction:
                # Waiting for a *person*, so the answer arrives by resume and
                # this node reports it. Anything else waits on work of its own
                # and the engine wakes it — marking that as human input would
                # complete the call the moment it came back, having asked
                # nobody and run nothing.
                scratch["awaiting_input"] = True
            AutomationAction.objects.filter(pk=action.pk).update(scratch=scratch)
            action.scratch = scratch
            waiting = output if isinstance(output, dict) else {}
            return WAITING, {**waiting, **self._for_the_person(call)}

        self._record_observation(action, result)
        return COMPLETED, output

    def do_work(self, action, data, single_step=False, plugin_dict=None):
        """What this action does, as opposed to how it was asked to do it.

        Separate from :meth:`execute` so the tool phases can wrap it. An action
        that would once have overridden ``execute`` overrides this instead, and
        then works identically whether a person drew it into a flow or a model
        asked for it.
        """
        raise NotImplementedError

    def run_tool_call(self, call, action, rows: list, arguments: dict) -> tuple[str, ToolResult | None, list]:
        """Do the action's own work with the model's arguments in place.

        The action reads its inputs in one of two ways, so the values are put
        where each kind will look. Actions whose editor inputs are expressions
        over the automation's data go through ``resolve_inputs`` and take
        ``_input_overrides``: the override bypasses resolution, because a value
        the model supplied is already the literal and resolving "Ship it" would
        read it as a data path. Actions configured with literal values read
        ``config`` directly and never see an override — for those the model's
        arguments *are* config, having been through the same form field an
        editor fills in.

        Both are set, and the config is put back afterwards: one call must not
        leave its arguments behind for the next.
        """
        from .engine import ActionPause
        from .tools import as_literal_config

        mappings = frozenset(getattr(self, "expression_mappings", frozenset()))
        configured = self.config
        self._input_overrides = arguments
        if arguments:
            self.config = {**(configured or {}), **as_literal_config(arguments, mappings)}
        try:
            state, output = self.do_work(action, rows)
        except ActionPause:
            # The engine's own pause signal — a rate limit, a backoff. It is not
            # an observation for the model; it means "run me again later", and
            # only the engine can do that.
            raise
        except ToolError as exc:
            # Raised by an action to say something to the model. Its text was
            # written for that, so it is passed on as it is.
            return COMPLETED, ToolResult(call_id=call.id, content=str(exc), is_error=True), []
        except Exception:
            # Everything else is a fault, and a fault's text is not written for
            # anybody: it can carry a query, a path, a token, or the body of
            # somebody else's response. It stays here, in the log, and the
            # model is told only that the tool did not work — which is the part
            # it can actually act on.
            logger.exception(
                "automation.tool.failed",
                extra={"automation_action_id": getattr(action, "pk", None), "tool": call.name},
            )
            return (
                COMPLETED,
                ToolResult(
                    call_id=call.id,
                    content="This tool failed. Try a different approach, or say that it could not be done.",
                    is_error=True,
                ),
                [],
            )
        finally:
            self.config = configured
            self._input_overrides = None

        if state == WAITING:
            return WAITING, None, output
        rows_out = output if isinstance(output, list) else [{"value": output}]
        if state == FAILED:
            # An action's failure payload is written for whoever operates this
            # — a traceback, a provider's response body, the query that broke.
            # It goes to the log. To say something to the model, an action
            # raises ``ToolError``, whose text was written for that.
            logger.warning(
                "automation.tool.returned_failure",
                extra={
                    "automation_action_id": getattr(action, "pk", None),
                    "tool": call.name,
                    "output": output,
                },
            )
            return (
                COMPLETED,
                ToolResult(
                    call_id=call.id,
                    content="This tool failed. Try a different approach, or say that it could not be done.",
                    is_error=True,
                ),
                [],
            )
        return COMPLETED, ToolResult(call_id=call.id, content=str(rows_out), rows=rows_out), rows_out

    # -- what a person sees ------------------------------------------------

    def _for_the_person(self, call) -> dict:
        """What the *Open tasks* page needs in order to be worth reading.

        Somebody approving a call is being asked to make a decision, and cannot
        make it from the fact that a decision is due. They need to know which
        tool wants to run, with what, and whether it can be taken back.
        """
        return {
            "tool": call.name,
            "arguments": call.arguments,
            "destructive": self.is_destructive(),
        }

    def _record_observation(self, action, result: ToolResult) -> None:
        """Leave the observation where the AI step will look for it.

        Read from the action rather than from a caller's snapshot: whatever ran
        may have written here in the meantime, and a nested step's conversation
        is not something to overwrite with a stale copy of the row.
        """
        scratch = {
            **(action.scratch or {}),
            "tool_result": {"call_id": result.call_id, "content": result.content, "is_error": result.is_error},
        }
        scratch.pop("awaiting_input", None)
        scratch.pop("awaiting_approval", None)
        AutomationAction.objects.filter(pk=action.pk).update(scratch=scratch)
        action.scratch = scratch

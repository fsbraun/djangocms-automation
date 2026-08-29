"""The tool contract: exposing automation actions to a model as callable tools.

An action and a tool are the same thing seen from two sides. An action's inputs
are filled in by an editor, as expressions over the automation's data; a tool's
are filled in by a model, as literal values. Everything else — what it does, what
it needs, what it returns — is identical, which is why a tool here is an
existing action plus a description of which of its inputs the model may supply.

That has a consequence worth stating plainly: **the schema is a hint to the
model, never a security boundary.** A model can ask for anything at all. What
keeps a tool call inside its bounds is the validation on this side, which runs
the model's arguments back through the action's own Django form and rejects any
key that was not offered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from django import forms

__all__ = [
    "TOOL_NAME_RE",
    "ToolCall",
    "ToolError",
    "ToolProvider",
    "ToolResult",
    "ToolSpec",
    "ToolValidationError",
    "schema_from_form",
    "validate_arguments",
]

from .llm import ToolCall

#: Tool names must satisfy every provider we support, so this is the
#: intersection rather than any one provider's rule.
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ToolError(Exception):
    """A tool could not run. Reported to the model, not raised at the engine."""


class ToolValidationError(ToolError):
    """The model's arguments did not satisfy the tool's schema.

    Deliberately recoverable. A model producing a wrong argument is ordinary
    behaviour, so this is handed back as an observation it can correct on the
    next turn rather than failing the automation.
    """


@dataclass(frozen=True)
class ToolSpec:
    """Everything the model is told about one tool.

    Pure metadata: no callable, no database state. That makes it cacheable,
    diffable, checkable at publication time, and renderable in the editor —
    and it keeps the description of a tool separate from the machinery that
    runs it.
    """

    name: str
    description: str
    #: JSON Schema for the arguments. Always an object with
    #: ``additionalProperties: false``, which strict tool-calling requires.
    parameters: dict[str, Any]
    #: Editor-side only; never sent to a provider.
    requires_approval: bool = False
    destructive: bool = False
    idempotent: bool = True

    def __post_init__(self):
        if not TOOL_NAME_RE.match(self.name or ""):
            raise ValueError(
                f"Tool name {self.name!r} is not usable: it must be 1-64 characters of "
                f"letters, digits, underscore or hyphen."
            )
        if not (self.description or "").strip():
            raise ValueError(
                f"Tool {self.name!r} needs a description. It is the only thing telling the "
                f"model when to reach for this tool, and the single biggest influence on "
                f"whether an agent uses it correctly."
            )

    def to_wire(self) -> dict[str, Any]:
        """Render as the provider-neutral tool definition the LLM layer sends."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    """The outcome of one tool invocation, on its way back to the model."""

    call_id: str
    #: What the model is shown. Kept small — see :meth:`truncate`.
    content: str | dict[str, Any]
    #: Canonical automation rows, for the data flowing on after the agent.
    rows: list[dict] = field(default_factory=list)
    is_error: bool = False

    def truncate(self, max_chars: int) -> ToolResult:
        """Return a copy whose content fits an agent's per-observation budget.

        A tool that returns a thousand rows would otherwise spend the whole
        context window on one observation. Truncation is visible in the text so
        the model knows it is seeing part of an answer.
        """
        text = self.content if isinstance(self.content, str) else str(self.content)
        if max_chars <= 0 or len(text) <= max_chars:
            return self
        kept = text[:max_chars]
        return ToolResult(
            call_id=self.call_id,
            content=f"{kept}\n… truncated, {len(text) - max_chars} more characters",
            rows=self.rows,
            is_error=self.is_error,
        )


@runtime_checkable
class ToolProvider(Protocol):
    """Anything that can present itself to an agent as a tool."""

    def get_tool_spec(self) -> ToolSpec: ...

    def invoke(self, call: ToolCall, *, action, rows: list[dict]) -> ToolResult:
        """Run the tool.

        :param call: What the model asked for. ``call.arguments`` is
            **untrusted** and must go through :func:`validate_arguments` first.
        :param action: The ``AutomationAction`` representing this tool call.
        :param rows: The automation data in scope, for resolving bound inputs.
        """


#: Django form field → JSON Schema. Ordered most specific first, because the
#: field classes inherit from one another (``EmailField`` is a ``CharField``).
_FIELD_SCHEMAS: list[tuple[type, dict[str, Any]]] = [
    (forms.EmailField, {"type": "string", "format": "email"}),
    (forms.URLField, {"type": "string", "format": "uri"}),
    (forms.SlugField, {"type": "string"}),
    (forms.JSONField, {"type": "object"}),
    (forms.BooleanField, {"type": "boolean"}),
    (forms.IntegerField, {"type": "integer"}),
    (forms.FloatField, {"type": "number"}),
    (forms.DecimalField, {"type": "number"}),
    (forms.DateTimeField, {"type": "string", "format": "date-time"}),
    (forms.DateField, {"type": "string", "format": "date"}),
    (forms.TimeField, {"type": "string", "format": "time"}),
    (forms.CharField, {"type": "string"}),
]


def _describe(bound_field) -> str:
    """Build a property description from a field's label and help text."""
    parts = [str(bound_field.label or "").strip(), str(bound_field.help_text or "").strip()]
    return " ".join(part for part in parts if part)


def _schema_for_field(field_instance) -> dict[str, Any]:
    """Map one Django form field onto a JSON Schema fragment."""
    # Choices come first: a ChoiceField is more usefully an enum than a string,
    # and telling the model the permitted values is the cheapest way to stop it
    # inventing one.
    choices = getattr(field_instance, "choices", None)
    if choices:
        values = [str(value) for value, _label in choices if value not in (None, "")]
        if isinstance(field_instance, forms.MultipleChoiceField):
            return {"type": "array", "items": {"type": "string", "enum": values}}
        return {"type": "string", "enum": values}

    for field_type, schema in _FIELD_SCHEMAS:
        if isinstance(field_instance, field_type):
            schema = dict(schema)
            max_length = getattr(field_instance, "max_length", None)
            if max_length and schema.get("type") == "string":
                schema["maxLength"] = max_length
            for attribute, key in (("min_value", "minimum"), ("max_value", "maximum")):
                value = getattr(field_instance, attribute, None)
                if value is not None and schema.get("type") in ("integer", "number"):
                    schema[key] = value
            return schema

    # An unrecognised field still has a string form, which is what the model
    # would produce anyway; the form itself does the real coercion.
    return {"type": "string"}


def schema_from_form(form_class: type[forms.Form], include: list[str] | None = None) -> dict[str, Any]:
    """Derive a JSON Schema object from an action's ``data_form``.

    The ``data_form`` already declares every input an action takes, with types,
    labels, help text and choices. That is a schema in all but name, so tools
    are derived from it rather than described a second time — an action author
    writes nothing extra to make their action callable by a model.

    :param include: The fields the model may fill. Anything omitted is bound by
        the editor and never shown to the model, which is what keeps the blast
        radius of a tool call as small as the person who added it chose.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, field_instance in form_class.base_fields.items():
        if include is not None and name not in include:
            continue
        schema = _schema_for_field(field_instance)
        description = _describe(field_instance)
        if description:
            schema["description"] = description
        properties[name] = schema
        if field_instance.required:
            required.append(name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        # Required by strict tool calling, and the reason a model cannot smuggle
        # in a field the editor did not offer.
        "additionalProperties": False,
    }


def validate_arguments(
    form_class: type[forms.Form],
    arguments: dict[str, Any],
    allowed: list[str],
) -> dict[str, Any]:
    """Validate and coerce a model's arguments through the action's own form.

    The same declaration that produced the schema does the checking, so a tool
    cannot drift from the action behind it. Coercion matters as much as
    rejection: a model may send ``"3"`` where an ``IntegerField`` is expected,
    and the form turns it into ``3``.

    :param allowed: The fields the tool exposed. Anything else is refused
        outright — the schema told the model what it could send, but only this
        decides what it may.
    :raises ToolValidationError: If anything is unknown, missing or invalid.
    """
    if not isinstance(arguments, dict):
        raise ToolValidationError("Arguments must be an object.")

    permitted = set(allowed)
    unexpected = sorted(set(arguments) - permitted)
    if unexpected:
        raise ToolValidationError(
            f"Unknown argument(s): {', '.join(unexpected)}. This tool accepts: {', '.join(sorted(permitted)) or 'nothing'}."
        )

    form = form_class(data={name: arguments.get(name) for name in permitted})
    # Fields the tool did not expose are the editor's to fill in, so they must
    # not be required of the model.
    for name in list(form.fields):
        if name not in permitted:
            del form.fields[name]

    if not form.is_valid():
        problems = "; ".join(f"{name}: {' '.join(errors)}" for name, errors in form.errors.items())
        raise ToolValidationError(f"Invalid argument(s): {problems}")

    return {name: value for name, value in form.cleaned_data.items() if name in permitted}

"""Literal-first values with safe ``{{ expression }}`` interpolation."""

import re

from django.forms import ValidationError

from .expressions import (
    ExpressionError,
    Literal,
    _resolve_variable,
    is_variable_reference,
    resolve_expression,
    validate_expression,
)


def resolve_path(context, path):
    """Resolve a dotted path safely through dicts/lists only.

    Delegates to the shared traversal in :mod:`.expressions` (single
    security posture: no object attribute access). Missing paths resolve
    to ``""``.
    """
    try:
        value = _resolve_variable(path, context)
    except ExpressionError:
        return ""
    return "" if value is None else value


VAR_PATTERN = re.compile(r"(?<!\\){{\s*([^{}\n]+?)\s*}}")
_OPEN_BRACES = re.compile(r"(?<!\\){{")


def referenced_paths(template):
    """Return item paths referenced by a value template, excluding literals."""
    return {
        expression
        for match in VAR_PATTERN.finditer(str(template or ""))
        if is_variable_reference(expression := match.group(1).strip()) and expression not in ("true", "false", "null")
    }


def render_value(template, context):
    """Render one literal-first value.

    Plain text is a string. A single, whole ``{{ expression }}`` returns the
    expression's native JSON-like type. Expressions surrounded by text are
    interpolated into a string. ``\\{{`` writes a literal ``{{``.
    """
    if isinstance(template, Literal):
        return template.value
    if template is None:
        raise ExpressionError("Value is None")
    template = str(template)
    matches = list(VAR_PATTERN.finditer(template))
    if len(_OPEN_BRACES.findall(template)) != len(matches):
        raise ExpressionError("Malformed value expression — use {{ field.name }}.")

    if len(matches) == 1 and matches[0].group(0).strip() == template.strip():
        return resolve_expression(matches[0].group(1).strip(), context)

    rendered = []
    end = 0
    for match in matches:
        rendered.append(template[end : match.start()].replace(r"\{{", "{{"))
        value = resolve_expression(match.group(1).strip(), context)
        rendered.append("" if value is None else str(value))
        end = match.end()
    rendered.append(template[end:].replace(r"\{{", "{{"))
    return "".join(rendered)


safe_render = render_value


def validate_value_template(template) -> bool:
    """Validate literal text and the expressions inside ``{{ … }}``.

    Values are not resolved here; references may legitimately be absent while
    authoring. Every unescaped ``{{`` must be closed and contain a supported
    safe expression.

    :raises ValidationError: If the template contains a malformed variable.
    """
    if template is None:
        raise ValidationError("Value is None")
    template = str(template)
    if len(_OPEN_BRACES.findall(template)) != len(VAR_PATTERN.findall(template)):
        raise ValidationError("Malformed value expression — use {{ field.name }}.")
    for expression in VAR_PATTERN.findall(template):
        validate_expression(expression.strip())
    return True


validate_template = validate_value_template

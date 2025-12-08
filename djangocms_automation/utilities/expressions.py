"""Simple expression resolver.

Supported forms:
        - Integer and float literals (e.g. 42, -3, 3.14, +0.5)
        - Quoted string literals with single or double quotes. Supports escape sequences: \n, \t, \r, \\ and \" / \'
        - Variable references composed of dotted identifiers (e.g. user.profile.age)
          resolved against a provided context dict. Each segment must match /[A-Za-z_][A-Za-z0-9_]*\n+      and traversal follows dictionary keys or object attributes. Lists / tuples can be
          traversed using an integer segment.

This intentionally does NOT execute arbitrary Python code or support operators – it is a
minimal safe resolver. Extend carefully if needed.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from django.forms import ValidationError

__all__ = ["ExpressionError", "resolve_expression", "is_number_literal", "is_string_literal", "validate_expression"]


class ExpressionError(ValidationError):
    """Raised for invalid expressions or resolution failures."""


_NUMBER_RE = re.compile(r"^[\s]*([+-]?)(?:((?:\d+))(?:\.(\d*))?|(?:\.(\d+)))[\s]*$")
_INT_ONLY_RE = re.compile(r"^[\s]*([+-]?)(\d+)[\s]*$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_number_literal(expr: str) -> bool:
    """Return True if expr is a valid int or float literal."""
    return bool(_NUMBER_RE.match(expr) or _INT_ONLY_RE.match(expr))


def is_string_literal(expr: str) -> bool:
    """Return True if expr is a quoted string (single or double quotes)."""
    if len(expr) < 2:
        return False
    if (expr[0], expr[-1]) in (("'", "'"), ('"', '"')):
        return True
    return False


def _parse_number(expr: str) -> int | float:
    expr = expr.strip()
    # Distinguish int vs float
    if _INT_ONLY_RE.match(expr):
        return int(expr)
    if _NUMBER_RE.match(expr):
        return float(expr)
    raise ExpressionError(f"Invalid numeric literal: {expr}")


_ESCAPE_MAP = {
    "\\n": "\n",
    "\\t": "\t",
    "\\r": "\r",
    '"': '"',
    "'": "'",
    "\\\\": "\\",
}


def _unescape(s: str) -> str:
    result = s
    for key, val in _ESCAPE_MAP.items():
        result = result.replace(key, val)
    return result


def _parse_string(expr: str) -> str:
    q = expr[0]
    if expr[-1] != q:
        raise ExpressionError("Unterminated string literal")
    inner = expr[1:-1]
    return _unescape(inner)


def _get_from_context(segment: str, current: Any) -> Any:
    # Numeric segment for list/tuple index
    if segment.isdigit():
        idx = int(segment)
        if isinstance(current, (list, tuple)):
            try:
                return current[idx]
            except IndexError as e:
                raise ExpressionError(f"Index {idx} out of range") from e
    # Dict key
    if isinstance(current, dict) and segment in current:
        return current[segment]
    # Object attribute
    if hasattr(current, segment):
        return getattr(current, segment)
    raise ExpressionError(f"Segment '{segment}' not found")


def _resolve_variable(expr: str, context: dict[str, Any]) -> Any:
    parts = expr.split(".")
    if not all(_IDENT_RE.match(p) or p.isdigit() for p in parts):
        raise ExpressionError(f"Invalid identifier path: {expr}")
    current: Any = context
    for seg in parts:
        current = _get_from_context(seg, current)
    return current


def validate_expression(expr: str) -> bool:
    """Validate expression syntax without resolving values.

    Returns True if the expression is syntactically valid (number literal,
    string literal, or valid identifier path). Raises ExpressionError if invalid.
    """
    if expr is None:
        raise ExpressionError("Expression is None")
    expr = str(expr).strip()
    if expr == "":
        raise ExpressionError("Empty expression")
    if is_string_literal(expr):
        q = expr[0]
        if expr[-1] != q:
            raise ExpressionError("Unterminated string literal")
        return True
    if is_number_literal(expr):
        return True
    parts = expr.split(".")
    if not all(_IDENT_RE.match(p) or p.isdigit() for p in parts):
        raise ExpressionError(f"Invalid identifier path: {expr}")
    return True


def resolve_expression(expr: str, context: dict[str, Any]) -> Any:
    """Resolve a simple expression against context.

    Examples:
            resolve_expression('42', ctx) -> 42
            resolve_expression('3.14', ctx) -> 3.14
            resolve_expression('\"hello\"', ctx) -> 'hello'
            resolve_expression('user.profile.age', ctx) -> value in context
            resolve_expression('items.0.name', ctx) -> first element's name
    """
    if expr is None:
        raise ExpressionError("Expression is None")
    expr = str(expr).strip()
    if expr == "":
        raise ExpressionError("Empty expression")
    if is_string_literal(expr):
        return _parse_string(expr)
    if is_number_literal(expr):
        return _parse_number(expr)
    # Variable path
    return _resolve_variable(expr, context)


# Convenience dataclass for compiled expressions (if future caching is desired)
@dataclass(slots=True)
class CompiledExpression:
    raw: str

    def evaluate(self, context: dict[str, Any]) -> Any:
        return resolve_expression(self.raw, context)


def compile_expression(expr: str) -> CompiledExpression:
    return CompiledExpression(expr)

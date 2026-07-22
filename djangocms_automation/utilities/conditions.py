"""Runtime evaluator for conditions built with ``ConditionBuilderWidget``.

The widget stores conditions as JSON::

    {
        "logic": "and" | "or",
        "conditions": [
            {"field": "status", "operator": "==", "value": "'active'"},
            {"field": "count", "operator": ">", "value": "5"}
        ]
    }

``field`` and ``value`` are expressions (see :mod:`.expressions`): number or
string literals, or dotted paths resolved against the automation data. A
missing path resolves to ``None`` instead of raising, so conditions never
crash a running automation.

The supported operators mirror ``widgets.ConditionBuilderWidget.operators``:
``==, !=, <, >, <=, >=, contains, not_contains, starts_with, ends_with,
in, not_in``.
"""

from __future__ import annotations

import json
from typing import Any

from .expressions import ExpressionError, resolve_expression

__all__ = ["evaluate"]


def _resolve(expr: Any, context: dict[str, Any]) -> Any:
    """Resolve an expression, returning ``None`` for missing paths/invalid input."""
    if expr is None:
        return None
    try:
        return resolve_expression(str(expr), context)
    except ExpressionError:
        return None


def _as_number(value: Any) -> float | None:
    """Return the value as a float if it is (or parses as) a number."""
    if isinstance(value, bool):  # bool is an int subclass; don't treat as number
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _compare(left: Any, right: Any, op: str) -> bool:
    """Ordering comparison with numeric coercion, falling back to strings."""
    l_num, r_num = _as_number(left), _as_number(right)
    if l_num is not None and r_num is not None:
        left, right = l_num, r_num
    else:
        left, right = str(left), str(right)
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    return left >= right


def _as_list(value: Any) -> list:
    """Coerce a value to a list for membership tests (comma-split for strings)."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [part.strip() for part in value.split(",")]
    return [value]


def _equal(left: Any, right: Any) -> bool:
    l_num, r_num = _as_number(left), _as_number(right)
    if l_num is not None and r_num is not None:
        return l_num == r_num
    return left == right or str(left) == str(right)


def _membership(left: Any, right: Any) -> bool:
    candidates = _as_list(right)
    return any(_equal(left, candidate) for candidate in candidates)


def evaluate_leaf(cond: dict[str, Any], context: dict[str, Any]) -> bool:
    """Evaluate a single ``{field, operator, value}`` condition."""
    op = cond.get("operator", "==")
    left = _resolve(cond.get("field"), context)
    right = _resolve(cond.get("value"), context)

    if op == "==":
        return _equal(left, right)
    if op == "!=":
        return not _equal(left, right)
    if op in ("<", ">", "<=", ">="):
        if left is None or right is None:
            return False
        return _compare(left, right, op)
    if op == "contains":
        return str(right) in str(left) if left is not None else False
    if op == "not_contains":
        return str(right) not in str(left) if left is not None else True
    if op == "starts_with":
        return str(left).startswith(str(right)) if left is not None else False
    if op == "ends_with":
        return str(left).endswith(str(right)) if left is not None else False
    if op == "in":
        return _membership(left, right)
    if op == "not_in":
        return not _membership(left, right)
    return False


def evaluate(condition: dict | str | None, data: list[dict] | None) -> bool:
    """Evaluate a ConditionBuilderWidget condition against automation data.

    The context is the first data row (if any), with the full list of rows
    additionally exposed as ``data``. An empty or missing condition
    evaluates to ``True`` (pass-through).

    :param condition: The condition dict (or its JSON string form).
    :param data: The automation data rows.
    :returns: The boolean outcome of the condition.
    """
    if isinstance(condition, str):
        try:
            condition = json.loads(condition)
        except json.JSONDecodeError:
            return True
    if not condition or not isinstance(condition, dict):
        return True
    conditions = condition.get("conditions") or []
    if not conditions:
        return True

    rows = data or []
    first_row = rows[0] if rows and isinstance(rows[0], dict) else {}
    context = {**first_row, "data": rows}

    results = (evaluate_leaf(cond, context) for cond in conditions)
    if condition.get("logic", "and") == "or":
        return any(results)
    return all(results)

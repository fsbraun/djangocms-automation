"""Tests for expression resolver in utilities.expressions."""

import pytest

from djangocms_automation.utilities.expressions import (
    resolve_expression,
    compile_expression,
    ExpressionError,
)


@pytest.fixture
def context():
    class Obj:
        def __init__(self):
            self.value = 99
            self.child = type("Child", (), {"leaf": "end"})()

    return {
        "answer": 42,
        "pi": 3.14159,
        "user": {"profile": {"age": 30, "name": "Alice"}},
        "items": [
            {"name": "first"},
            {"name": "second"},
        ],
        "obj": Obj(),
    }


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("42", 42),
        ("-5", -5),
        ("3.14", 3.14),
        ("+0.5", 0.5),
    ],
)
def test_number_literals(expr, expected, context):
    assert resolve_expression(expr, context) == expected


@pytest.mark.parametrize(
    "expr, expected",
    [
        ('"hello"', "hello"),
        ("'world'", "world"),
        ('"escape \\n tab"', "escape \n tab"),
    ],
)
def test_string_literals(expr, expected, context):
    assert resolve_expression(expr, context) == expected


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("answer", 42),
        ("user.profile.age", 30),
        ("items.0.name", "first"),
        ("obj.value", 99),
        ("obj.child.leaf", "end"),
    ],
)
def test_variable_paths(expr, expected, context):
    assert resolve_expression(expr, context) == expected


@pytest.mark.parametrize("expr", ["", None, "9invalid", "user..profile", "items.bad.name"])
def test_invalid_expressions(expr, context):
    with pytest.raises(ExpressionError):
        resolve_expression(expr, context)


def test_compile_expression(context):
    c = compile_expression("user.profile.name")
    assert c.evaluate(context) == "Alice"

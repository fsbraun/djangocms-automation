"""Unit tests for the condition evaluator (utilities.conditions)."""

import pytest

from djangocms_automation.utilities.conditions import evaluate


DATA = [{"status": "active", "count": 5, "name": "Alice", "tags": ["a", "b"]}]


@pytest.mark.parametrize(
    "field, operator, value, expected",
    [
        # equality (with literal quoting and numeric coercion)
        ("status", "==", "'active'", True),
        ("status", "==", "'inactive'", False),
        ("status", "!=", "'inactive'", True),
        ("count", "==", "5", True),
        ("count", "!=", "5", False),
        # ordering with numeric coercion
        ("count", ">", "3", True),
        ("count", "<", "3", False),
        ("count", ">=", "5", True),
        ("count", "<=", "4", False),
        # string operators
        ("name", "contains", "'lic'", True),
        ("name", "not_contains", "'xyz'", True),
        ("name", "starts_with", "'Al'", True),
        ("name", "ends_with", "'ce'", True),
        ("name", "starts_with", "'ce'", False),
        # membership (comma-separated literals)
        ("status", "in", "'active, pending'", True),
        ("status", "not_in", "'archived, deleted'", True),
        ("count", "in", "'1, 5, 9'", True),
        # field resolved against list value
        ("tags.0", "==", "'a'", True),
    ],
)
def test_operators(field, operator, value, expected):
    condition = {"logic": "and", "conditions": [{"field": field, "operator": operator, "value": value}]}
    assert evaluate(condition, DATA) is expected


def test_and_logic():
    condition = {
        "logic": "and",
        "conditions": [
            {"field": "status", "operator": "==", "value": "'active'"},
            {"field": "count", "operator": ">", "value": "10"},
        ],
    }
    assert evaluate(condition, DATA) is False


def test_or_logic():
    condition = {
        "logic": "or",
        "conditions": [
            {"field": "status", "operator": "==", "value": "'active'"},
            {"field": "count", "operator": ">", "value": "10"},
        ],
    }
    assert evaluate(condition, DATA) is True


def test_missing_field_is_falsy():
    condition = {"logic": "and", "conditions": [{"field": "missing", "operator": ">", "value": "1"}]}
    assert evaluate(condition, DATA) is False
    # ...but equality against a missing field can match a missing value expression
    condition = {"logic": "and", "conditions": [{"field": "missing", "operator": "==", "value": "also_missing"}]}
    assert evaluate(condition, DATA) is True  # None == None


def test_full_data_accessible_via_data_key():
    condition = {"logic": "and", "conditions": [{"field": "data.0.name", "operator": "==", "value": "'Alice'"}]}
    assert evaluate(condition, DATA) is True


@pytest.mark.parametrize("condition", [None, {}, {"conditions": []}, "", "not json"])
def test_empty_or_invalid_conditions_pass_through(condition):
    assert evaluate(condition, DATA) is True


def test_json_string_condition():
    condition = '{"logic": "and", "conditions": [{"field": "count", "operator": ">=", "value": "5"}]}'
    assert evaluate(condition, DATA) is True


def test_empty_data():
    condition = {"logic": "and", "conditions": [{"field": "status", "operator": "==", "value": "'active'"}]}
    assert evaluate(condition, []) is False


@pytest.mark.parametrize(
    "field, operator, value, expected",
    [
        # unknown operator is safely falsy
        ("status", "regex", "'a.*'", False),
        # missing operator defaults to equality
        ("status", None, "'active'", True),
        # string ordering fallback (both sides non-numeric)
        ("name", "<", "'Bob'", True),
        # booleans follow Python equality (True == 1); for ordering they
        # fall back to string comparison ("True" > "0")
        ("flag", "==", "1", True),
        ("flag", ">", "0", True),
        # membership against a non-list, non-string value
        ("count", "in", "5", True),
        # field expression is None
        (None, "==", "'x'", False),
    ],
)
def test_operator_edges(field, operator, value, expected):
    data = [{"status": "active", "count": 5, "name": "Alice", "flag": True}]
    cond = {"field": field, "value": value}
    if operator is not None:
        cond["operator"] = operator
    condition = {"logic": "and", "conditions": [cond]}
    assert evaluate(condition, data) is expected


def test_non_dict_first_row_uses_empty_context():
    condition = {"logic": "and", "conditions": [{"field": "data.0", "operator": "==", "value": "'x'"}]}
    assert evaluate(condition, ["x"]) is True

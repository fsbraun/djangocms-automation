"""Tests for literal-first value templates (utilities.templates)."""

import pytest
from django.forms import ValidationError

from djangocms_automation.utilities.expressions import ExpressionError
from djangocms_automation.utilities.templates import referenced_paths, resolve_path, safe_render, validate_template

CONTEXT = {
    "user": {"name": "Alice", "age": 30},
    "items": [{"sku": "A"}, {"sku": "B"}],
    "count": 5,
    "empty": None,
}


class TestResolvePath:
    def test_dict_traversal(self):
        assert resolve_path(CONTEXT, "user.name") == "Alice"

    def test_list_index(self):
        assert resolve_path(CONTEXT, "items.1.sku") == "B"

    def test_missing_path_returns_empty_string(self):
        assert resolve_path(CONTEXT, "user.missing") == ""
        assert resolve_path(CONTEXT, "nope.deep") == ""

    def test_none_value_returns_empty_string(self):
        assert resolve_path(CONTEXT, "empty") == ""

    def test_object_attribute_access_blocked(self):
        class Obj:
            secret = "leak"

        assert resolve_path({"obj": Obj()}, "obj.secret") == ""


class TestSafeRender:
    def test_single_variable_preserves_type(self):
        # A template that is exactly one variable returns the raw value.
        assert safe_render("{{ count }}", CONTEXT) == 5
        assert safe_render("{{ user }}", CONTEXT) == {"name": "Alice", "age": 30}

    def test_multi_variable_renders_string(self):
        result = safe_render("{{ user.name }} is {{ user.age }}", CONTEXT)
        assert result == "Alice is 30"

    def test_missing_variable_raises(self):
        with pytest.raises(ExpressionError, match="missing"):
            safe_render("Hi {{ user.missing }}!", CONTEXT)

    def test_plain_text_passthrough(self):
        assert safe_render("no variables here", CONTEXT) == "no variables here"
        assert safe_render("welcome", CONTEXT) == "welcome"
        assert safe_render("42", CONTEXT) == "42"

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("{{ 42 }}", 42),
            ("{{ -3.5 }}", -3.5),
            ("{{ true }}", True),
            ("{{ false }}", False),
            ("{{ null }}", None),
        ],
    )
    def test_whole_expression_preserves_scalar_type(self, value, expected):
        assert safe_render(value, CONTEXT) == expected

    def test_mixed_value_is_always_text(self):
        assert safe_render("Count: {{ count }}", CONTEXT) == "Count: 5"
        assert safe_render("Value: {{ null }}", CONTEXT) == "Value: "

    def test_escaped_open_braces_are_literal(self):
        assert safe_render(r"Write \{{ subject }}", CONTEXT) == "Write {{ subject }}"


def test_referenced_paths_only_returns_data_references():
    assert referenced_paths("{{ user.name }} / {{ 42 }} / {{ true }}") == {"user.name"}


class TestValidateTemplate:
    @pytest.mark.parametrize(
        "template",
        ["plain text", "Hello {{ user.name }}", "{{ a }} and {{ b.c }}", "{{ 42 }}", "{{ true }}", ""],
    )
    def test_valid(self, template):
        assert validate_template(template) is True

    @pytest.mark.parametrize("template", ["{{ unclosed", "{{ bad-chars! }}", "{{ }}"])
    def test_malformed_variable_raises(self, template):
        with pytest.raises(ValidationError):
            validate_template(template)

    def test_none_raises(self):
        with pytest.raises(ValidationError):
            validate_template(None)

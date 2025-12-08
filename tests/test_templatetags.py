"""Tests for automation_tags template filters."""

from types import SimpleNamespace


from djangocms_automation.templatetags.automation_tags import (
    then_branch,
    else_branch,
    format_paragraphs,
)


class TestThenBranchFilter:
    """Tests for the then_branch filter."""

    def test_filters_then_plugins(self):
        """Test that then_branch returns only ThenPlugin instances."""
        plugins = [
            SimpleNamespace(plugin_type="ThenPlugin"),
            SimpleNamespace(plugin_type="ElsePlugin"),
            SimpleNamespace(plugin_type="ThenPlugin"),
            SimpleNamespace(plugin_type="ActionPlugin"),
        ]
        result = list(then_branch(plugins))
        assert len(result) == 2
        assert all(p.plugin_type == "ThenPlugin" for p in result)

    def test_returns_empty_for_no_then_plugins(self):
        """Test that then_branch returns empty when no ThenPlugin exists."""
        plugins = [
            SimpleNamespace(plugin_type="ElsePlugin"),
            SimpleNamespace(plugin_type="ActionPlugin"),
        ]
        result = list(then_branch(plugins))
        assert result == []

    def test_returns_empty_for_none_input(self):
        """Test that then_branch handles None input."""
        result = then_branch(None)
        assert result == []

    def test_returns_empty_for_empty_list(self):
        """Test that then_branch handles empty list."""
        result = list(then_branch([]))
        assert result == []


class TestElseBranchFilter:
    """Tests for the else_branch filter."""

    def test_filters_else_plugins(self):
        """Test that else_branch returns only ElsePlugin instances."""
        plugins = [
            SimpleNamespace(plugin_type="ThenPlugin"),
            SimpleNamespace(plugin_type="ElsePlugin"),
            SimpleNamespace(plugin_type="ElsePlugin"),
            SimpleNamespace(plugin_type="ActionPlugin"),
        ]
        result = list(else_branch(plugins))
        assert len(result) == 2
        assert all(p.plugin_type == "ElsePlugin" for p in result)

    def test_returns_empty_for_no_else_plugins(self):
        """Test that else_branch returns empty when no ElsePlugin exists."""
        plugins = [
            SimpleNamespace(plugin_type="ThenPlugin"),
            SimpleNamespace(plugin_type="ActionPlugin"),
        ]
        result = list(else_branch(plugins))
        assert result == []

    def test_returns_empty_for_none_input(self):
        """Test that else_branch handles None input."""
        result = else_branch(None)
        assert result == []

    def test_returns_empty_for_empty_list(self):
        """Test that else_branch handles empty list."""
        result = list(else_branch([]))
        assert result == []


class TestFormatParagraphsFilter:
    """Tests for the format_paragraphs filter."""

    def test_single_paragraph(self):
        """Test formatting a single paragraph."""
        result = format_paragraphs("Hello world")
        assert result == "<p>Hello world</p>"

    def test_multiple_paragraphs(self):
        """Test formatting multiple paragraphs."""
        result = format_paragraphs("First\nSecond\nThird")
        assert result == "<p>First</p><p>Second</p><p>Third</p>"

    def test_empty_lines_are_skipped(self):
        """Test that empty lines are skipped."""
        result = format_paragraphs("First\n\nSecond\n\n\nThird")
        assert result == "<p>First</p><p>Second</p><p>Third</p>"

    def test_whitespace_is_stripped(self):
        """Test that whitespace is stripped from paragraphs."""
        result = format_paragraphs("  First  \n  Second  ")
        assert result == "<p>First</p><p>Second</p>"

    def test_html_is_escaped(self):
        """Test that HTML is properly escaped."""
        result = format_paragraphs("<script>alert('xss')</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_empty_string(self):
        """Test formatting an empty string."""
        result = format_paragraphs("")
        assert result == ""

    def test_only_whitespace(self):
        """Test formatting string with only whitespace."""
        result = format_paragraphs("   \n   \n   ")
        assert result == ""

    def test_result_is_marked_safe(self):
        """Test that result is marked as safe HTML."""
        from django.utils.safestring import SafeString

        result = format_paragraphs("Hello")
        assert isinstance(result, SafeString)

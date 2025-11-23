from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def then_branch(value):
    if value:
        return (plugin for plugin in value if plugin.plugin_type == "ThenPlugin")
    return []


@register.filter
def else_branch(value):
    if value:
        return (plugin for plugin in value if plugin.plugin_type == "ElsePlugin")
    return []


@register.filter
def format_paragraphs(text):
    """Format text into HTML paragraphs."""
    paragraphs = text.split("\n")
    formatted = mark_safe("".join(f"<p>{escape(p.strip())}</p>" for p in paragraphs if p.strip()))
    return formatted
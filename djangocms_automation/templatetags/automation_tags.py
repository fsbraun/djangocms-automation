from django import template

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

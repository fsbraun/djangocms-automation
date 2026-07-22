import re

from django.forms import ValidationError

from .expressions import ExpressionError, _resolve_variable


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


VAR_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")


def safe_render(template, context):
    # Find all variables in the template
    matches = list(VAR_PATTERN.finditer(template))

    # Case 1: Template consists ONLY of a single variable
    if len(matches) == 1 and matches[0].group(0).strip() == template.strip():
        var_name = matches[0].group(1)
        return resolve_path(context, var_name)

    # Case 2: Multiple variables → normal rendering as string
    def replacer(match):
        path = match.group(1)
        value = resolve_path(context, path)
        return str(value) if value is not None else ""

    return VAR_PATTERN.sub(replacer, template)


_OPEN_BRACES = re.compile(r"{{")


def validate_template(template) -> bool:
    """Validate ``{{ dotted.path }}`` template syntax.

    Every ``{{`` must belong to a well-formed variable reference.

    :raises ValidationError: If the template contains a malformed variable.
    """
    if template is None:
        raise ValidationError("Template is None")
    template = str(template)
    if len(_OPEN_BRACES.findall(template)) != len(VAR_PATTERN.findall(template)):
        raise ValidationError("Malformed template variable — use {{ dotted.path }}.")
    return True

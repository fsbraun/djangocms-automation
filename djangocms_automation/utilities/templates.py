import re


def resolve_path(context, path):
    """Resolve a dotted path safely through dicts/lists only."""
    parts = path.split(".")
    value = context

    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list):
            try:
                idx = int(part)
                value = value[idx]
            except (ValueError, IndexError):
                return ""  # invalid index
        else:
            # Block attribute access to objects (security)
            return ""
        if value is None:
            return ""
    return value


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

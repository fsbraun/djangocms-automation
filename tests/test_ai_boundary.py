"""The AI surface is one-directional: it imports core, core never imports it.

Stated in ``djangocms_automation/ai/__init__.py`` and enforced here, because a
rule about imports that nothing checks is a rule that lasts until the next
convenient shortcut. It is what allows the package to be lifted into a
separately installable app later as a rename, and what lets a project that never
uses an LLM leave the optional dependency uninstalled.
"""

import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "djangocms_automation"
AI = PACKAGE / "ai"


def core_modules():
    for path in PACKAGE.rglob("*.py"):
        if AI in path.parents or "migrations" in path.parts:
            continue
        yield path


def imported_names(path):
    """Every module name a file imports, absolute and relative alike."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            yield ("." * node.level) + (node.module or "")


def test_no_core_module_imports_the_ai_package():
    offenders = []
    for path in core_modules():
        for name in imported_names(path):
            if "djangocms_automation.ai" in name or name.startswith(".ai") or name == ".ai":
                offenders.append(f"{path.relative_to(PACKAGE.parent)} imports {name}")
    assert not offenders, "core must not depend on the AI surface:\n  " + "\n  ".join(offenders)


def test_only_the_ai_package_names_litellm():
    """The whole point of the wrapper: one file knows which provider library is used."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if AI in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if "litellm" in text:
            offenders.append(str(path.relative_to(PACKAGE.parent)))
    assert not offenders, "litellm must only be named inside djangocms_automation/ai:\n  " + "\n  ".join(offenders)


def test_the_ai_app_registers_its_own_plugin():
    """Core does not register it, so the app must — that is why ``ai`` is an app."""
    from cms.plugin_pool import plugin_pool

    plugin = plugin_pool.get_plugin("LLMAction")
    assert plugin.__module__ == "djangocms_automation.ai.cms_plugins"


def test_removing_the_ai_package_would_not_break_the_engine():
    """The engine, transitions and models must import with no AI surface present."""
    import importlib

    for name in ("djangocms_automation.engine", "djangocms_automation.transitions", "djangocms_automation.models"):
        module = importlib.import_module(name)
        assert "djangocms_automation.ai" not in getattr(module, "__dict__", {})

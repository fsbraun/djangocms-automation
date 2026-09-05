"""Capture executable CMS trees and reconstruct them without the live editor."""

import json
from copy import deepcopy
from types import MethodType

from cms.plugin_pool import plugin_pool
from cms.utils.plugins import downcast_plugins, get_plugins_as_layered_tree
from django.core.serializers.json import DjangoJSONEncoder

from . import __version__


def capture_definition(trigger, placeholder):
    plugins = list(downcast_plugins(list(placeholder.get_plugins()), [placeholder]))
    roots = get_plugins_as_layered_tree(plugins)

    def freeze(node):
        fields = {field.attname: field.value_from_object(node) for field in node._meta.concrete_fields}
        if hasattr(node, "default_outputs"):
            fields["outputs"] = node.outputs or node.default_outputs
        if hasattr(node, "budget"):
            from dataclasses import asdict

            fields["config"] = {**(node.config or {}), **asdict(node.budget())}
            fields["config"]["llm_timeout"] = int((node.config or {}).get("llm_timeout") or 120)
        from .engine import _resolve_timeout
        from .retry import DEFAULT_RETRY_POLICY

        policy = getattr(node, "retry_policy", None) or DEFAULT_RETRY_POLICY
        return {
            "type": node.plugin_type,
            "version": getattr(type(node), "execution_version", 1),
            "fields": fields,
            "timeout": _resolve_timeout(node),
            "retry": {
                name: getattr(policy, name)
                for name in ("max_attempts", "backoff_seconds", "backoff_multiplier", "max_backoff_seconds", "jitter")
            },
            "retry_on": [f"{exc.__module__}.{exc.__qualname__}" for exc in policy.retry_on],
            "children": [freeze(child) for child in node.child_plugin_instances or []],
        }

    definition = {
        "format": 1,
        "runtime": __version__,
        "automation": {
            "id": trigger.automation_content.automation_id,
            "content_id": trigger.automation_content_id,
            "name": trigger.automation_content.automation.name,
        },
        "trigger": {
            "type": trigger.type,
            "slot": trigger.slot,
            "schema": trigger.data_schema,
            "config": {
                key: value for key, value in (trigger.config or {}).items() if key not in {"token", "signing_secret"}
            },
        },
        "fields": trigger.automation_content.data_fields,
        "output": trigger.automation_content.output_fields,
        "nodes": [freeze(node) for node in roots],
    }
    return json.loads(json.dumps(definition, cls=DjangoJSONEncoder))


def load_definition(definition):
    if not definition or definition.get("format") != 1:
        raise ValueError("The executed definition is unavailable or unsupported.")
    result = {}

    def thaw(spec):
        cls = plugin_pool.get_plugin(spec["type"]).model
        if getattr(cls, "execution_version", 1) != spec["version"]:
            raise ValueError(f"Unsupported executor version for {spec['type']}.")
        fields = deepcopy(spec["fields"])
        converted = {
            f.attname: f.to_python(fields[f.attname]) for f in cls._meta.concrete_fields if f.attname in fields
        }
        node = cls(**converted)
        node._frozen_definition = True
        if hasattr(node, "default_outputs"):
            node.default_outputs = deepcopy(fields.get("outputs", {}))
        node.timeout_seconds = spec.get("timeout")
        from django.utils.module_loading import import_string

        from .retry import RetryPolicy

        node.retry_policy = RetryPolicy(
            **spec["retry"], retry_on=tuple(import_string(name) for name in spec["retry_on"])
        )
        node.child_plugin_instances = [thaw(child) for child in spec["children"]]
        node.get_plugin_instance = MethodType(lambda self: (self, plugin_pool.get_plugin(self.plugin_type)), node)
        if hasattr(node, "uuid"):
            result[node.uuid] = node
        return node

    from .engine import _link_tree

    roots = [thaw(spec) for spec in definition["nodes"]]
    _link_tree(roots)
    return result


def instance_plugins(instance):
    if instance.trace_redacted:
        raise ValueError("Execution payloads were redacted; this run cannot be resumed or replayed.")
    return load_definition(instance.definition)

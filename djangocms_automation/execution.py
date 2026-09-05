"""Single-item execution, explicit writes, and lease-fenced trace recording."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction


def item_data(value=None) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(  # noqa: TRY004 — input validation error at the JSON boundary
            "Supply one item as a JSON object. Put lists in named fields; batches are not supported."
        )
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder, allow_nan=False))


@dataclass(frozen=True)
class ExecutionContext:
    """Private item snapshot and durable scope of one action occurrence."""

    action: object
    item: dict

    def __post_init__(self):
        object.__setattr__(self, "item", item_data(self.item))

    @property
    def variables(self):
        return {**deepcopy(self.item), "loop": deepcopy((self.action.context or {}).get("loop", {}))}

    def record(self, kind, payload):
        record_trace(self.action, kind, payload)


def record_trace(action, kind, payload):
    """Append observations only while this occurrence still owns the action."""
    from .instances import RUNNING, AutomationAction, ExecutionTrace

    with transaction.atomic():
        current = AutomationAction.objects.select_for_update().get(pk=action.pk)
        if current.lease_id != action.lease_id or current.state != RUNNING:
            raise RuntimeError("Execution no longer owns its lease.")
        ExecutionTrace.objects.create(
            action=current,
            occurrence=current.lease_id,
            kind=kind,
            scope=current.context,
            payload=item_data(payload),
        )


def save_working_state(action, scratch):
    """Persist resumable executor state with the same fencing as item output."""
    from .instances import RUNNING, WAITING, AutomationAction

    payload = item_data(scratch)
    from .instances import ExecutionTrace

    with transaction.atomic():
        current = AutomationAction.objects.select_for_update().get(pk=action.pk)
        if current.lease_id != action.lease_id or current.state not in (RUNNING, WAITING):
            raise RuntimeError("Execution no longer owns its lease.")
        current.scratch = payload
        current.save(update_fields=["scratch"])
        ExecutionTrace.objects.create(
            action=current,
            occurrence=current.lease_id,
            kind="working_state",
            scope=current.context,
            payload={"state": payload},
        )
    action.scratch = payload


def place_results(plugin, action, item, results, *, record=True):
    """Apply declared result writes; never infer writes by comparing JSON."""
    output = item_data(item)
    writes = {}
    results = item_data(results)
    if record:
        record_trace(action, "results", {"results": results})
    for name, binding in (plugin.outputs or plugin.default_outputs).items():
        if name not in results or binding is None:
            continue
        target = binding.get("field", "")
        if not target.isidentifier() or target == "loop":
            raise ValueError(f"Invalid output field: {target!r}")
        if target in writes:
            raise ValueError(f"Several results write field '{target}'. Choose separate destinations.")
        mode = binding.get("mode", "replace")
        value = deepcopy(results[name])
        if mode == "append":
            previous = output.get(target, [])
            if not isinstance(previous, list):
                raise ValueError(f"Cannot append to non-list field '{target}'.")
            output[target] = [*previous, value]
        elif mode == "replace":
            output[target] = value
        else:
            raise ValueError(f"Unknown write mode '{mode}'.")
        schema = (action.automation_instance.definition.get("fields", {}).get(target, {}) or {}).get("schema")
        if schema:
            from jsonschema import validate

            validate(output[target], schema)
        writes[target] = {"mode": mode, "value": value}
    action.writes = writes
    action._declared_results = results
    if record:
        record_trace(action, "writes", {"writes": writes})
    return item_data(output)

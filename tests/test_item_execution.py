"""The single-item contract, frozen execution, and durable evidence."""

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool
from django.contrib.contenttypes.models import ContentType

from djangocms_automation import engine
from djangocms_automation.instances import COMPLETED, FAILED, AutomationAction, ExecutionTrace
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger, BaseActionPluginModel


class ItemValueModel(BaseActionPluginModel):
    default_outputs = {"value": {"field": "value"}}

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, context, inputs):
        return {"value": inputs.get("value", context.item.get("value", 0) + 1)}


@plugin_pool.register_plugin
class ItemValuePlugin(CMSPluginBase):
    model = ItemValueModel
    name = "Item value"
    render_template = "djangocms_automation/plugins/action.html"


@pytest.fixture
def flow(db, admin_user, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    automation = Automation.objects.create(name="Items", is_active=True)
    content = AutomationContent.objects.with_user(admin_user).create(automation=automation)
    trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click")
    placeholder = Placeholder.objects.create(
        content_type=ContentType.objects.get_for_model(content), object_id=content.pk, slot="start"
    )
    return trigger, placeholder


def node(placeholder, kind="ItemValuePlugin", **kwargs):
    return add_plugin(placeholder, kind, "en", **kwargs)


def test_item_preserves_fields_and_records_resolved_inputs(flow):
    trigger, placeholder = flow
    node(placeholder, config={"value": "customer.age"})
    run = trigger.trigger_execution({"customer": {"age": 7}})
    run.refresh_from_db()
    assert run.status == COMPLETED
    assert run.data == {"customer": {"age": 7}, "value": 7}
    assert ExecutionTrace.objects.get(kind="inputs").payload == {"used": {"value": 7}}
    assert ExecutionTrace.objects.filter(kind="transition", payload__to=COMPLETED).get().payload["after"] == run.data


def test_frozen_definition_survives_plugin_deletion(flow):
    trigger, placeholder = flow
    step = node(placeholder, config={"value": "42"})
    run = trigger.trigger_execution({"keep": True}, start=False)
    step.delete()
    engine.run_action(run.automationaction_set.get().pk)
    run.refresh_from_db()
    assert run.status == COMPLETED
    assert run.data == {"keep": True, "value": 42}


def test_run_survives_deleting_authored_content(flow):
    trigger, placeholder = flow
    node(placeholder)
    run = trigger.trigger_execution({}, start=False)
    trigger.automation_content.delete()
    run.refresh_from_db()
    assert run.automation_content_id is None
    engine.run_action(run.automationaction_set.get().pk)
    run.refresh_from_db()
    assert run.status == COMPLETED and run.data == {"value": 1}
    assert "Items" in str(run)


def test_for_each_appends_and_preserves_original_item(flow):
    trigger, placeholder = flow
    loop = node(placeholder, "AutomationForEach", iterable="entries")
    node(
        placeholder,
        target=loop,
        config={"value": "loop.entry"},
        outputs={"value": {"field": "results", "mode": "append"}},
    )
    run = trigger.trigger_execution({"entries": [3, 1, 2], "customer": "Alice"})
    run.refresh_from_db()
    assert run.status == COMPLETED
    assert run.data == {"entries": [3, 1, 2], "customer": "Alice", "results": [3, 1, 2]}
    assert ExecutionTrace.objects.filter(kind="loop").count() == 4


@pytest.mark.parametrize("conflict", [False, True])
def test_parallel_paths_join_declared_writes(flow, conflict):
    trigger, placeholder = flow
    split = node(placeholder, "AutomationSplit")
    for name in ("left", "left" if conflict else "right"):
        path = node(placeholder, "AutomationPath", target=split)
        node(placeholder, target=path, config={"value": "1"}, outputs={"value": {"field": name}})
    run = trigger.trigger_execution({"keep": True})
    run.refresh_from_db()
    assert run.status == (FAILED if conflict else COMPLETED)
    if not conflict:
        assert run.data == {"keep": True, "left": 1, "right": 1}
    else:
        assert AutomationAction.objects.filter(error_detail__contains="both write").exists()


def test_batch_input_rejected(flow):
    trigger, placeholder = flow
    node(placeholder)
    with pytest.raises(ValueError, match="batches"):
        trigger.trigger_execution([{"value": 1}])


def test_ai_answer_is_a_field_and_tool_inputs_are_recorded(flow, monkeypatch):
    from djangocms_automation.ai import llm
    from djangocms_automation.ai.llm import LLMResult
    from djangocms_automation.tools import ToolCall

    trigger, placeholder = flow
    ai = node(placeholder, "AIStep", config={"model": "dummy/echo", "prompt": "Hello {{ name }}"})
    node(placeholder, target=ai, tool_name="counter")
    replies = iter(
        [
            LLMResult(
                text="", json=None, model="dummy/echo", tool_calls=[ToolCall(id="one", name="counter", arguments={})]
            ),
            LLMResult(text="Done", json=None, model="dummy/echo"),
        ]
    )
    monkeypatch.setattr(llm, "complete", lambda **kwargs: next(replies))
    run = trigger.trigger_execution({"name": "Alice"})
    run.refresh_from_db()
    assert run.status == COMPLETED
    assert run.data["name"] == "Alice"
    assert run.data["answer"]["text"] == "Done"
    assert ExecutionTrace.objects.filter(kind="inputs").count() >= 3


def test_wait_records_submission_without_losing_item(flow, admin_user):
    trigger, placeholder = flow
    node(placeholder, "UserInputAction", config={"note": "Review {{ name }}"})
    run = trigger.trigger_execution({"name": "Alice"})
    action = run.automationaction_set.get()
    engine.resume_action(action.pk, admin_user, {"approved": True})
    run.refresh_from_db()
    assert run.data == {"name": "Alice", "response": {"approved": True}}
    assert ExecutionTrace.objects.filter(payload__metadata__resumed_by=admin_user.pk).exists()


def test_query_keeps_matches_in_list_field(flow, settings):
    settings.AUTOMATION_ALLOWED_MODELS = ["auth.User"]
    trigger, placeholder = flow
    node(placeholder, "QueryModelAction", config={"model": "auth.User", "filters": {"username": "'not-present'"}})
    run = trigger.trigger_execution({"name": "Alice"})
    run.refresh_from_db()
    assert run.status == COMPLETED
    assert run.data == {"name": "Alice", "records": []}


def test_append_is_one_value_and_duplicate_delivery_is_a_noop(flow):
    trigger, placeholder = flow
    node(placeholder, config={"value": "entries"}, outputs={"value": {"field": "collected", "mode": "append"}})
    run = trigger.trigger_execution({"entries": [1, 2]})
    action = run.automationaction_set.get()
    count = action.trace.count()
    engine.run_action(action.pk)
    run.refresh_from_db()
    assert run.data == {"entries": [1, 2], "collected": [[1, 2]]}
    assert action.trace.count() == count


def test_for_each_captures_list_before_body_changes_it(flow):
    trigger, placeholder = flow
    loop = node(placeholder, "AutomationForEach", iterable="entries")
    node(placeholder, target=loop, config={"value": "loop.entry"}, outputs={"value": {"field": "entries"}})
    run = trigger.trigger_execution({"entries": [4, 7]})
    run.refresh_from_db()
    assert run.status == COMPLETED
    assert run.data == {"entries": 7}
    assert run.automationaction_set.get(plugin_ptr=loop.uuid).scratch["entries"] == [4, 7]


def test_nested_for_each_restores_outer_scope(flow):
    trigger, placeholder = flow
    outer = node(placeholder, "AutomationForEach", iterable="groups")
    inner = node(placeholder, "AutomationForEach", target=outer, iterable="loop.entry")
    node(
        placeholder,
        target=inner,
        config={"value": "loop.entry"},
        outputs={"value": {"field": "values", "mode": "append"}},
    )
    node(
        placeholder,
        target=outer,
        config={"value": "loop.index"},
        outputs={"value": {"field": "indices", "mode": "append"}},
    )
    run = trigger.trigger_execution({"groups": [[4, 5], [6]]})
    run.refresh_from_db()
    assert run.status == COMPLETED
    assert run.data == {"groups": [[4, 5], [6]], "values": [4, 5, 6], "indices": [0, 1]}


def test_definition_is_immutable_and_public_output_does_not_discard_item(flow):
    trigger, placeholder = flow
    trigger.automation_content.output_fields = ["value"]
    trigger.automation_content.save()
    node(placeholder, config={"value": "7"})
    run = trigger.trigger_execution({"private": "retained"})
    run.refresh_from_db()
    assert run.output == {"value": 7}
    assert run.data == {"private": "retained", "value": 7}
    run.definition = {}
    with pytest.raises(ValueError, match="immutable"):
        run.save()


def test_trace_retention_clears_scopes_and_refuses_replay(flow):
    import datetime

    from django.utils.timezone import now

    from djangocms_automation.instances import AutomationInstance

    trigger, placeholder = flow
    loop = node(placeholder, "AutomationForEach", iterable="entries")
    node(placeholder, target=loop, config={"value": "loop.entry"})
    run = trigger.trigger_execution({"entries": ["private-loop-entry"]})
    action = run.automationaction_set.filter(parent__isnull=False).get()
    assert action.trace.filter(scope__loop__entry="private-loop-entry").exists()
    AutomationInstance.objects.filter(pk=run.pk).update(
        finished=now() - datetime.timedelta(days=10), updated=now() - datetime.timedelta(days=10)
    )
    assert AutomationInstance.redact_payloads(1) == 1
    run.refresh_from_db()
    assert run.trace_redacted and not run.definition and not run.data
    for event in ExecutionTrace.objects.filter(action__automation_instance=run):
        assert event.redacted and event.payload == {} and event.scope == {}
    with pytest.raises(ValueError, match="redacted"):
        engine.instance_plugins(run)


def test_stale_occurrence_cannot_record_or_place_authoritative_output(flow):
    from djangocms_automation.execution import record_trace, save_working_state
    from djangocms_automation.instances import PENDING, RUNNING
    from djangocms_automation.transitions import transition_action

    trigger, placeholder = flow
    node(placeholder)
    run = trigger.trigger_execution({}, start=False)
    stale = engine.claim_action(run.automationaction_set.get().pk)
    transition_action(stale.pk, PENDING, allowed_from=(RUNNING,), require_lease=stale.lease_id)
    current = engine.claim_action(stale.pk)
    assert current.lease_id != stale.lease_id
    with pytest.raises(RuntimeError, match="lease"):
        record_trace(stale, "results", {"value": 99})
    with pytest.raises(RuntimeError, match="lease"):
        save_working_state(stale, {"private": "stale"})
    assert transition_action(stale.pk, COMPLETED, allowed_from=(RUNNING,), require_lease=stale.lease_id) is None


def test_frozen_timeout_does_not_follow_changed_settings(flow, settings):
    trigger, placeholder = flow
    node(placeholder)
    settings.AUTOMATION_ACTION_TIMEOUT = None
    run = trigger.trigger_execution({}, start=False)
    settings.AUTOMATION_ACTION_TIMEOUT = 30
    plugin = next(iter(engine.instance_plugins(run).values()))
    assert engine._resolve_timeout(plugin) is None


def test_trace_events_cannot_be_edited(flow):
    trigger, placeholder = flow
    node(placeholder)
    trigger.trigger_execution({})
    event = ExecutionTrace.objects.first()
    event.payload = {"edited": True}
    with pytest.raises(ValueError, match="immutable"):
        event.save()


def test_mutating_context_cannot_bypass_declared_writes(flow, monkeypatch):
    def perform(self, context, inputs):
        context.item["customer"]["name"] = "Changed without a write"
        return {"value": 5}

    monkeypatch.setattr(ItemValueModel, "perform", perform)
    trigger, placeholder = flow
    node(placeholder)
    run = trigger.trigger_execution({"customer": {"name": "Alice"}})
    run.refresh_from_db()
    assert run.data == {"customer": {"name": "Alice"}, "value": 5}


def test_result_can_be_observed_without_saving_it(flow):
    trigger, placeholder = flow
    node(placeholder, outputs={"value": None})
    run = trigger.trigger_execution({"keep": True})
    run.refresh_from_db()
    assert run.data == {"keep": True}
    assert run.automationaction_set.get().writes == {}
    assert ExecutionTrace.objects.get(kind="results").payload == {"results": {"value": 1}}


def test_destination_schema_is_frozen_and_validated(flow):
    trigger, placeholder = flow
    content = trigger.automation_content
    content.data_fields = {"value": {"label": "Count", "schema": {"type": "integer"}}}
    content.save()
    node(placeholder, config={"value": "'wrong type'"})
    run = trigger.trigger_execution({}, start=False)
    content.data_fields = {}
    content.save()
    engine.run_action(run.automationaction_set.get().pk)
    run.refresh_from_db()
    assert run.status == FAILED
    assert ExecutionTrace.objects.get(kind="results").payload == {"results": {"value": "wrong type"}}
    assert not run.automationaction_set.get().writes


def test_redaction_winning_replay_race_cannot_restore_payloads(flow, monkeypatch):
    import datetime

    from django.utils.timezone import now

    from djangocms_automation.instances import AutomationInstance

    trigger, placeholder = flow
    node(placeholder, config={"value": "missing"})
    run = trigger.trigger_execution({"private": "do not restore"})
    action = run.automationaction_set.get()
    assert action.state == FAILED
    AutomationInstance.objects.filter(pk=run.pk).update(updated=now() - datetime.timedelta(days=10))
    load = engine.instance_plugins

    def load_then_redact(instance):
        plugins = load(instance)
        assert AutomationInstance.redact_payloads(1) == 1
        return plugins

    monkeypatch.setattr(engine, "instance_plugins", load_then_redact)
    with pytest.raises(ValueError, match="redacted"):
        engine.replay_action(action.pk)
    assert run.automationaction_set.count() == 1
    action.refresh_from_db()
    assert action.input_data is None


def test_field_picker_and_summary_keep_literals_out(flow, rf, admin_user):
    from django.contrib import admin

    from djangocms_automation.cms_plugins import MailAction
    from djangocms_automation.data_editor import data_summary

    trigger, placeholder = flow
    trigger.config = {"data_schema": {"type": "object", "properties": {"email": {"type": "string"}}}}
    trigger.save()
    step = node(placeholder, "MailAction", config={"recipient_email": "email", "subject": "'Hi'", "body": "Hello"})
    request = rf.get("/")
    request.user = admin_user
    form = MailAction(MailAction.model, admin.site).get_form(request, step)
    assert "Use data from" in str(form(instance=step)["recipient_email"])
    assert data_summary(step)["uses"] == "email"
    assert "delivery" in data_summary(step)["produces"]

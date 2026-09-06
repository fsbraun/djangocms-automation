"""Editor metadata is a catalogue of possible fields, never a payload preview."""

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from cms.plugin_pool import plugin_pool
from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from djangocms_automation.admin import AutomationTriggerAdmin
from djangocms_automation.cms_plugins import ActionPlugin
from djangocms_automation.data_editor import automation_fields, available_fields
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
from djangocms_automation.widgets import SchemaWidget


@pytest.fixture
def catalogue_flow(db, admin_user):
    automation = Automation.objects.create(name="Catalogue")
    content = AutomationContent.objects.with_user(admin_user).create(automation=automation)
    trigger = AutomationTrigger.objects.create(
        automation_content=content,
        type="code",
        slot="start",
        config={
            "data_schema": {
                "type": "object",
                "properties": {
                    "customer": {
                        "type": "object",
                        "properties": {
                            "email": {"type": "string", "default": "PRIVATE DEFAULT", "examples": ["PRIVATE EXAMPLE"]},
                        },
                        "additionalProperties": False,
                    }
                },
                "additionalProperties": False,
            }
        },
    )
    placeholder = Placeholder.objects.create(
        content_type=ContentType.objects.get_for_model(content),
        object_id=content.pk,
        slot="start",
    )
    return trigger, placeholder


def step(placeholder, kind="MailAction", **kwargs):
    return add_plugin(placeholder, kind, "en", intent=kwargs.pop("intent", "Send a message"), **kwargs)


def test_catalogue_unions_starting_and_mapped_nested_fields_without_values(catalogue_flow):
    trigger, placeholder = catalogue_flow
    step(placeholder, config={"body": "PRIVATE BODY"}, outputs={"delivery": {"field": "receipt"}})
    model_admin = AutomationTriggerAdmin(AutomationTrigger, admin.site)
    with CaptureQueriesContext(connection) as queries:
        html = model_admin.automation_fields(trigger)
    for name in ("customer", "customer.email", "receipt", "receipt.sent", "receipt.recipient"):
        assert f"<code>{name}</code>" in html
    assert "PRIVATE" not in html
    assert "delivery.sent" not in html
    assert "may be absent" in html
    assert not any(
        table in query["sql"].lower()
        for query in queries
        for table in (
            "djangocms_automation_automationinstance",
            "djangocms_automation_automationaction",
            "djangocms_automation_executiontrace",
        )
    )
    assert trigger.data_schema["properties"].keys() == {"customer"}


def test_catalogue_is_scoped_to_its_trigger_path(catalogue_flow):
    trigger, placeholder = catalogue_flow
    other = Placeholder.objects.create(
        content_type=placeholder.content_type,
        object_id=placeholder.object_id,
        slot="other",
    )
    AutomationTrigger.objects.create(
        automation_content=trigger.automation_content,
        type="code",
        slot="other",
        config={"data_schema": {"properties": {"other_input": {"type": "string"}}}},
    )
    step(other, outputs={"delivery": {"field": "other_output"}})
    trigger.automation_content.data_fields = {"other_output": {"label": "Saved by another path"}}
    trigger.automation_content.save()
    names = {row["name"] for row in automation_fields(trigger=trigger)}
    assert names == {"customer", "customer.email"}


def test_ai_schema_is_reused_and_destinations_prefix_nested_paths(catalogue_flow, rf):
    trigger, placeholder = catalogue_flow
    ai = step(
        placeholder,
        "AIStep",
        config={
            "output_schema": {
                "type": "object",
                "properties": {"score": {"type": "number"}},
                "additionalProperties": False,
            }
        },
        outputs={"answer": {"field": "assessment"}},
    )
    rows = {row["name"]: row for row in automation_fields(trigger=trigger)}
    assert rows["assessment.score"]["type"] == "Number"
    plugin_class = plugin_pool.get_plugin("AIStep")
    plugin = plugin_class(plugin_class.model, admin.site)
    request = rf.get("/")
    request.user = None
    form_class = plugin.get_form(request, ai)
    assert isinstance(form_class.base_fields["output_schema"].widget, SchemaWidget)
    fieldsets = dict(plugin.get_fieldsets(request, ai))
    assert fieldsets["Intent"]["fields"] == ("intent", "model")
    assert fieldsets["Uses"]["fields"] == ("prompt", "system_prompt")
    assert "output_schema" in fieldsets["Produces"]["fields"]


def test_ai_schema_ignored_with_tools_is_not_advertised(catalogue_flow):
    trigger, placeholder = catalogue_flow
    ai = step(placeholder, "AIStep", config={"output_schema": {"properties": {"score": {"type": "number"}}}})
    step(placeholder, target=ai)
    rows = {row["name"]: row for row in automation_fields(trigger=trigger)}
    assert "answer.score" not in rows
    assert rows["answer"]["type"] == "Object"
    assert rows["answer.text"]["type"] == "Text"
    assert "delivery.sent" in rows


def test_append_wraps_result_in_a_list_and_picker_does_not_offer_array_wildcards(catalogue_flow):
    trigger, placeholder = catalogue_flow
    step(placeholder, outputs={"delivery": {"field": "receipts", "mode": "append"}})
    consumer = step(placeholder)
    rows = {row["name"]: row for row in automation_fields(trigger=trigger)}
    assert rows["receipts"]["type"] == "List"
    assert rows["receipts[].sent"]["type"] == "Yes / no"
    choices = available_fields(consumer)
    assert "receipts" in choices
    assert "Earlier step; may be absent" in choices["receipts"]
    assert not any("[]" in name for name in choices)
    assert "delivery" not in choices  # not its own output


def test_picker_marks_future_outputs(catalogue_flow):
    _trigger, placeholder = catalogue_flow
    consumer = step(placeholder)
    step(placeholder, outputs={"delivery": {"field": "later"}})
    assert "Later step; may be absent" in available_fields(consumer)["later.sent"]


def test_duplicate_destinations_keep_both_producers_and_types(catalogue_flow):
    trigger, placeholder = catalogue_flow
    step(placeholder, intent="Mail", outputs={"delivery": {"field": "result"}})
    step(placeholder, "UpdateModelAction", intent="Update", outputs={"count": {"field": "result"}})
    rows = [row for row in automation_fields(trigger=trigger) if row["name"] == "result"]
    assert {(row["source"], row["type"]) for row in rows} == {("Mail", "Object"), ("Update", "Whole number")}


def test_dynamic_start_and_submission_are_explicit(catalogue_flow):
    trigger, placeholder = catalogue_flow
    trigger.config = {}
    trigger.save()
    step(placeholder, "UserInputAction")
    rows = {row["name"]: row for row in automation_fields(trigger=trigger)}
    assert rows["*"]["type"] == "Unknown / dynamic"
    assert "additional fields possible" in rows["response"]["type"]


def test_catalogue_escapes_field_names_and_producers(catalogue_flow):
    trigger, placeholder = catalogue_flow
    step(placeholder, intent="<script>producer</script>", outputs={"delivery": {"field": "<unsafe>"}})
    html = AutomationTriggerAdmin(AutomationTrigger, admin.site).automation_fields(trigger)
    assert "<script>" not in html and "<unsafe>" not in html
    assert "&lt;unsafe&gt;" in html and "&lt;script&gt;" in html


def test_declared_fields_are_not_claimed_as_initialized(catalogue_flow):
    trigger, placeholder = catalogue_flow
    step(placeholder, "UpdateModelAction", outputs={"count": {"field": "counter"}})
    trigger.automation_content.data_fields = {"counter": {"schema": {"type": "integer"}}}
    trigger.automation_content.save()
    rows = {row["name"]: row for row in automation_fields(trigger=trigger)}
    assert rows["counter"]["availability"] == "Declared, not initialized"


@pytest.mark.parametrize(
    "kind", ["MailAction", "UserInputAction", "CreateModelAction", "UpdateModelAction", "QueryModelAction", "AIStep"]
)
def test_action_forms_have_uses_and_produces(kind, rf):
    plugin_class = plugin_pool.get_plugin(kind)
    plugin = plugin_class(plugin_class.model, admin.site)
    request = rf.get("/")
    request.user = None
    sections = dict(plugin.get_fieldsets(request))
    assert "Uses" in sections and "Produces" in sections
    assert "collapse" in sections["Uses"].get("classes", ())
    assert "collapse" in sections["Produces"].get("classes", ())
    if kind != "AIStep":
        assert "result_structure" in sections["Produces"]["fields"]
        assert "result_structure" in plugin.readonly_fields


def test_action_without_io_shows_empty_sections(rf):
    plugin = ActionPlugin(ActionPlugin.model, admin.site)
    request = rf.get("/")
    request.user = None
    sections = dict(plugin.get_fieldsets(request))
    assert sections["Uses"]["description"] == "No inputs needed."
    assert "collapse" in sections["Uses"]["classes"]
    assert "Produces" in sections
    assert "collapse" in sections["Produces"]["classes"]
    assert "No fields produced." in plugin.result_structure()


def test_custom_action_fieldsets_cannot_drop_uses_or_produces(rf):
    class CustomAction(ActionPlugin):
        fieldsets = [
            ("Intent", {"fields": ("intent",)}),
            ("Comment", {"fields": ("comment",)}),
        ]

    plugin = CustomAction(CustomAction.model, admin.site)
    request = rf.get("/")
    request.user = None
    sections = dict(plugin.get_fieldsets(request))
    assert "Uses" in sections
    assert "Produces" in sections
    assert "collapse" in sections["Uses"]["classes"]
    assert "collapse" in sections["Produces"]["classes"]
    assert sections["Produces"]["fields"] == ("result_structure", "outputs")


def test_query_and_create_shapes_use_model_metadata_only(catalogue_flow, settings):
    trigger, placeholder = catalogue_flow
    settings.AUTOMATION_ALLOWED_MODELS = ["auth.User"]
    step(placeholder, "CreateModelAction", config={"model": "auth.User"})
    step(placeholder, "QueryModelAction", config={"model": "auth.User", "fields": "email,is_active"})
    rows = {row["name"]: row for row in automation_fields(trigger=trigger)}
    assert rows["created_id"]["type"] == "Whole number"
    assert rows["records[].pk"]["type"] == "Whole number"
    assert rows["records[].email"]["type"] == "Text"
    assert rows["records[].is_active"]["type"] == "Yes / no"
    assert "records[].password" not in rows


def test_trigger_change_page_renders_catalogue_read_only(catalogue_flow, admin_client):
    trigger, placeholder = catalogue_flow
    step(placeholder)
    response = admin_client.get(reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert '<table class="automation-field-catalogue">' in html
    assert "delivery.sent" in html
    assert 'name="automation_fields"' not in html


def test_disabled_output_is_not_listed(catalogue_flow):
    trigger, placeholder = catalogue_flow
    step(placeholder, outputs={"delivery": None})
    assert not any(row["name"].startswith("delivery") for row in automation_fields(trigger=trigger))


def test_nested_nullable_list_shape_is_listed(catalogue_flow):
    trigger, placeholder = catalogue_flow
    step(
        placeholder,
        "AIStep",
        config={
            "output_schema": {
                "type": "object",
                "properties": {
                    "reviews": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "object",
                            "properties": {"score": {"type": "number"}},
                            "additionalProperties": False,
                        },
                    }
                },
                "additionalProperties": False,
            }
        },
    )
    rows = {row["name"]: row for row in automation_fields(trigger=trigger)}
    assert rows["answer.reviews"]["type"] == "List / Null"
    assert rows["answer.reviews[].score"]["type"] == "Number"


def test_fixed_result_shape_cannot_be_edited(catalogue_flow, rf):
    _trigger, placeholder = catalogue_flow
    instance = step(placeholder)
    cls = plugin_pool.get_plugin("MailAction")
    plugin = cls(cls.model, admin.site)
    request = rf.get("/")
    request.user = None
    form_class = plugin.get_form(request, instance)
    assert "result_structure" not in form_class.base_fields
    assert "output_schema" not in form_class.base_fields
    assert "delivery.sent" in plugin.result_structure(instance)


@pytest.mark.parametrize("as_tool", [False, True])
def test_email_edit_modal_renders_uses_and_produces(catalogue_flow, admin_client, as_tool):
    _trigger, placeholder = catalogue_flow
    parent = step(placeholder, "AIStep") if as_tool else None
    instance = step(placeholder, target=parent)
    response = admin_client.get(reverse("admin:cms_placeholder_edit_plugin", args=[instance.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert ">Uses</h2>" in html
    assert ">Produces</h2>" in html
    assert 'name="outputs"' in html
    assert "delivery.sent" in html


def test_email_add_modal_renders_uses_and_produces(catalogue_flow, admin_client):
    _trigger, placeholder = catalogue_flow
    response = admin_client.get(
        reverse("admin:cms_placeholder_add_plugin"),
        {
            "cms_path": "/",
            "placeholder_id": placeholder.pk,
            "plugin_type": "MailAction",
            "plugin_language": "en",
            "plugin_position": 1,
        },
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert ">Uses</h2>" in html
    assert ">Produces</h2>" in html
    assert 'name="outputs"' in html
    assert "delivery.sent" in html

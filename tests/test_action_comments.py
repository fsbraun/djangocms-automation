from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.utils.html import escape

from djangocms_automation.models import (
    BaseActionPluginModel,
    ConditionalPluginModel,
    LoopPluginModel,
    SplitPluginModel,
)


def render_action(template, *, comment, edit_mode, uses="", produces=""):
    request = RequestFactory().get("/")
    request.user = None
    request.toolbar = SimpleNamespace(edit_mode_active=edit_mode)
    instance = SimpleNamespace(
        child_plugin_instances=[],
        intent="Notify the customer",
        actor="Support team",
        comment=comment,
        messages=[],
        needs_approval=False,
        tool_messages=[],
        tool_name="",
    )
    return render_to_string(
        template,
        {
            "instance": instance,
            "title": "Action",
            "tools": [],
            "approval_tools": [],
            "data_summary": {"uses": uses, "produces": produces},
        },
        request=request,
    )


@pytest.mark.parametrize(
    "template",
    [
        "djangocms_automation/plugins/action.html",
        "djangocms_automation/plugins/ai_step.html",
        "djangocms_automation/plugins/tool.html",
    ],
)
def test_action_comments_are_available_as_closed_click_details_in_edit_mode(template):
    comment = 'Explain "why" & keep <this> safe.'

    html = render_action(template, comment=comment, edit_mode=True)

    assert f'data-comment="{escape(comment)}"' in html
    assert 'class="automation-comment-trigger"' in html
    assert '<details class="automation-detail automation-comment">' in html
    assert '<summary class="automation-comment-trigger" aria-label="Comment">' in html
    assert f"<div>{escape(comment)}</div>" in html
    assert 'role="tooltip"' not in html


@pytest.mark.parametrize("plugin", ["action", "ai_step", "tool"])
@pytest.mark.parametrize(
    "field,label,icon",
    [("uses", "Uses", "bi-box-arrow-in-up"), ("produces", "Produces", "bi-box-arrow-down")],
)
def test_data_details_are_closed_and_accessible_by_icon(plugin, field, label, icon):
    value = 'input.customer <name> & "email"'
    html = render_action(
        f"djangocms_automation/plugins/{plugin}.html",
        comment="",
        edit_mode=False,
        **{field: value},
    )

    details = html.split(f'<details class="automation-detail automation-{field}">', 1)[1].split("</details>", 1)[0]
    summary, panel = details.split("</summary>", 1)
    assert f'<summary aria-label="{label}">' in summary
    assert f'<use href="#{icon}">' in summary
    assert value not in summary
    assert f"<strong>{label}</strong><div>{escape(value)}</div>" in panel
    other_field = "produces" if field == "uses" else "uses"
    assert f"automation-{other_field}" not in html


@pytest.mark.parametrize("edit_mode", [False, None])
def test_action_comments_are_not_exposed_outside_edit_mode(edit_mode):
    comment = "An internal editor note"

    html = render_action("djangocms_automation/plugins/action.html", comment=comment, edit_mode=edit_mode)

    assert "data-comment=" not in html
    assert comment not in html
    assert "automation-comment-trigger" not in html
    assert "automation-step-details" not in html


def test_blank_action_comments_do_not_create_an_indicator():
    html = render_action("djangocms_automation/plugins/action.html", comment="", edit_mode=True)

    assert "data-comment=" not in html
    assert "automation-comment-trigger" not in html
    assert "automation-step-details" not in html


@pytest.mark.parametrize(
    "template",
    [
        "djangocms_automation/plugins/action.html",
        "djangocms_automation/plugins/ai_step.html",
        "djangocms_automation/plugins/tool.html",
    ],
)
def test_action_intent_is_the_heading_and_type_is_the_subtitle(template):
    html = render_action(template, comment="", edit_mode=False)

    assert '<div class="automation-intent">Notify the customer</div>' in html or (
        '<span class="automation-intent">Notify the customer</span>' in html
    )
    type_start = html.index('class="automation-type"')
    if template.endswith("tool.html"):
        assert html.index("Action", type_start) > type_start
    else:
        icon = html.index('<svg width="14" height="14">', type_start)
        assert html.index("Action", icon) > icon


@pytest.mark.parametrize(
    "model",
    [BaseActionPluginModel, ConditionalPluginModel, LoopPluginModel, SplitPluginModel],
)
def test_intent_is_required_for_every_executable_node(model):
    assert model._meta.get_field("intent").blank is False


def test_actor_is_empty_by_default():
    assert BaseActionPluginModel().actor == ""


def test_actor_is_a_plain_uppercase_action_detail():
    html = render_action("djangocms_automation/plugins/action.html", comment="", edit_mode=False)

    actor = html[html.index('class="modifier actor"') :]
    assert "Support team" in actor
    assert "modifier-arrow" not in actor

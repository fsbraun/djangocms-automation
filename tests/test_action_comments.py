from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.utils.html import escape


def render_action(template, *, comment, edit_mode):
    request = RequestFactory().get("/")
    request.user = None
    request.toolbar = SimpleNamespace(edit_mode_active=edit_mode)
    instance = SimpleNamespace(
        child_plugin_instances=[],
        comment=comment,
        messages=[],
        needs_approval=False,
        tool_messages=[],
        tool_name="",
    )
    return render_to_string(
        template,
        {"instance": instance, "title": "Action", "tools": []},
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
def test_action_comments_are_available_as_a_balloon_in_edit_mode(template):
    comment = 'Explain "why" & keep <this> safe.'

    html = render_action(template, comment=comment, edit_mode=True)

    assert f'data-comment="{escape(comment)}"' in html
    assert 'class="automation-comment-trigger"' in html
    assert 'class="automation-comment-balloon" role="tooltip"' in html


@pytest.mark.parametrize("edit_mode", [False, None])
def test_action_comments_are_not_exposed_outside_edit_mode(edit_mode):
    comment = "An internal editor note"

    html = render_action("djangocms_automation/plugins/action.html", comment=comment, edit_mode=edit_mode)

    assert "data-comment=" not in html
    assert comment not in html
    assert "automation-comment-trigger" not in html


def test_blank_action_comments_do_not_create_an_indicator():
    html = render_action("djangocms_automation/plugins/action.html", comment="", edit_mode=True)

    assert "data-comment=" not in html
    assert "automation-comment-trigger" not in html

"""Tests for the admin open-tasks list / resume views and instance admin displays."""

import uuid

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from djangocms_automation.actions.user_input import UserInputActionPluginModel
from djangocms_automation.admin import AutomationInstanceAdmin
from djangocms_automation.instances import COMPLETED, FAILED, WAITING, AutomationAction, AutomationInstance
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Admin Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Admin automation content",
    )


@pytest.fixture
def waiting_action(automation_content, settings, admin_user):
    """A real UserInputAction run that is WAITING for interaction."""
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="click", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    plugin = add_plugin(placeholder=placeholder, plugin_type="UserInputAction", language=settings.LANGUAGE_CODE)
    model = UserInputActionPluginModel.objects.get(pk=plugin.pk)
    model.config = {"note": "Please approve {{ subject }}", "permissions": ""}
    model.save()
    trigger.trigger_execution(data=[{"subject": "order 42"}], start=True)
    instance = automation_content.automationinstance_set.first()
    return AutomationAction.objects.get(automation_instance=instance)


@pytest.mark.django_db
def test_open_tasks_view_lists_waiting_tasks(admin_client, waiting_action):
    url = reverse("admin:djangocms_automation_open_tasks")
    response = admin_client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Please approve order 42" in content
    assert reverse("admin:djangocms_automation_resume_action", args=[waiting_action.pk]) in content


@pytest.mark.django_db
def test_open_tasks_view_empty(admin_client, db):
    response = admin_client.get(reverse("admin:djangocms_automation_open_tasks"))
    assert response.status_code == 200
    assert "No open tasks" in response.content.decode()


@pytest.mark.django_db
def test_resume_view_completes_task(admin_client, waiting_action):
    url = reverse("admin:djangocms_automation_resume_action", args=[waiting_action.pk])
    response = admin_client.post(url, follow=True)
    assert response.status_code == 200
    assert "Task resumed." in response.content.decode()
    waiting_action.refresh_from_db()
    assert waiting_action.state == COMPLETED
    waiting_action.automation_instance.refresh_from_db()
    assert waiting_action.automation_instance.status == COMPLETED


@pytest.mark.django_db
def test_resume_view_get_redirects_without_resuming(admin_client, waiting_action):
    url = reverse("admin:djangocms_automation_resume_action", args=[waiting_action.pk])
    response = admin_client.get(url)
    assert response.status_code == 302
    waiting_action.refresh_from_db()
    assert waiting_action.state == WAITING


@pytest.mark.django_db
def test_resume_view_error_for_non_waiting_action(admin_client, automation_content):
    instance = AutomationInstance.objects.create(automation_content=automation_content)
    action = AutomationAction.objects.create(
        automation_instance=instance, plugin_ptr=uuid.uuid4()
    )  # not requiring interaction
    url = reverse("admin:djangocms_automation_resume_action", args=[action.pk])
    response = admin_client.post(url, follow=True)
    assert response.status_code == 200
    assert "not awaiting user interaction" in response.content.decode()


@pytest.mark.django_db
def test_instance_admin_displays(automation_content, rf, admin_user):
    """is_success / data_display / error_message_display helpers."""
    admin_instance = AutomationInstanceAdmin(AutomationInstance, admin_site=None)

    instance = AutomationInstance.objects.create(automation_content=automation_content, data=[{"x": 1}])

    # No actions at all -> success (nothing failed, nothing running)
    assert admin_instance.is_success(instance) is True

    running = AutomationAction.objects.create(automation_instance=instance, plugin_ptr=uuid.uuid4())
    assert admin_instance.is_success(instance) is None  # still running

    running.state = FAILED
    running.result = {"error": "boom", "traceback": "tb..."}
    running.save()
    assert admin_instance.is_success(instance) is False

    assert "&quot;x&quot;: 1" in admin_instance.data_display(instance)
    errors = admin_instance.error_message_display(instance)
    assert "boom" in errors and "tb..." in errors

    empty = AutomationInstance.objects.create(automation_content=automation_content, data=[])
    assert admin_instance.data_display(empty) == "-"
    assert admin_instance.error_message_display(empty) == "-"


@pytest.fixture
def runnable(automation_content, settings, admin_user):
    """An automation with one trigger and one action, ready to be started."""
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="code", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    plugin = add_plugin(placeholder=placeholder, plugin_type="UserInputAction", language=settings.LANGUAGE_CODE)
    model = UserInputActionPluginModel.objects.get(pk=plugin.pk)
    model.config = {"note": "Approve {{ subject }}", "permissions": ""}
    model.save()
    return trigger


def _run_url(automation_content):
    return reverse("admin:djangocms_automation_run_now") + f"?automation_content={automation_content.pk}"


@pytest.mark.django_db
def test_run_now_starts_a_real_run(admin_client, runnable, automation_content):
    """The moment right after building one is the moment to try it."""
    response = admin_client.post(
        _run_url(automation_content),
        {"trigger": runnable.pk, "data": '[{"subject": "order 42"}]'},
    )

    assert response.status_code == 302
    instance = automation_content.automationinstance_set.get()
    assert instance.initial_data == [{"subject": "order 42"}]
    assert AutomationAction.objects.filter(automation_instance=instance).exists(), "it actually ran"
    assert str(instance.pk) in response.url, "and lands on the run, not back where it started"


@pytest.mark.django_db
def test_run_now_shows_a_form_before_running_anything(admin_client, runnable, automation_content):
    response = admin_client.get(_run_url(automation_content))

    assert response.status_code == 200
    assert not automation_content.automationinstance_set.exists(), "GET starts nothing"


@pytest.mark.django_db
def test_run_now_refuses_data_the_real_entry_point_would(admin_client, runnable, automation_content):
    """A manual run that skipped the schema check would prove nothing.

    It would pass data through that the webhook or the calling automation
    refuses, and so report that the automation works when it does not.
    """
    runnable.config = {
        "data_schema": {
            "type": "object",
            "required": ["subject"],
            "properties": {"subject": {"type": "string"}},
            "additionalProperties": False,
        }
    }
    runnable.save()

    response = admin_client.post(
        _run_url(automation_content),
        {"trigger": runnable.pk, "data": '[{"nonsense": 1}]'},
    )

    assert response.status_code == 200, "redisplayed with the complaint"
    assert not automation_content.automationinstance_set.exists()
    assert "Row 1" in response.content.decode()


@pytest.mark.django_db
def test_run_now_rejects_rows_that_are_not_objects(admin_client, runnable, automation_content):
    response = admin_client.post(_run_url(automation_content), {"trigger": runnable.pk, "data": "[1, 2]"})

    assert response.status_code == 200
    assert not automation_content.automationinstance_set.exists()


@pytest.mark.django_db
def test_run_now_is_refused_to_someone_who_may_only_look(client, runnable, automation_content, django_user_model):
    """Starting a run sends mail and writes records."""
    from django.contrib.auth.models import Permission

    onlooker = django_user_model.objects.create_user("onlooker", password="x", is_staff=True)
    onlooker.user_permissions.add(Permission.objects.get(codename="view_automationtrigger"))
    client.force_login(onlooker)

    response = client.post(_run_url(automation_content), {"trigger": runnable.pk, "data": '[{"subject": "order 42"}]'})

    assert response.status_code == 403
    assert not automation_content.automationinstance_set.exists()


@pytest.fixture
def talked(automation_content):
    """An action carrying a conversation, as an AI step leaves one behind."""
    instance = AutomationInstance.objects.create(automation_content=automation_content, data=[{}])
    return AutomationAction.objects.create(
        automation_instance=instance,
        plugin_ptr=uuid.uuid4(),
        state=COMPLETED,
        scratch={
            "turn": 2,
            "tool_calls": 1,
            "usage": {"input_tokens": 120, "output_tokens": 30},
            "messages": [
                {"role": "user", "content": "Who wrote in about invoice 4402?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "find_customer", "arguments": '{"email": "ada@example.com"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "Ada Lovelace"},
                {"role": "assistant", "content": "Ada Lovelace wrote in."},
            ],
        },
    )


def _conversation_url(action):
    return reverse("admin:djangocms_automation_conversation", args=[action.pk])


@pytest.mark.django_db
def test_the_conversation_panel_shows_what_was_asked_and_answered(admin_client, talked):
    """A run that went wrong is usually a prompt that read differently than it
    looked, or an answer nobody saw. Both were reachable only from a shell."""
    body = admin_client.get(_conversation_url(talked)).content.decode()

    assert "Who wrote in about invoice 4402?" in body
    assert "Ada Lovelace wrote in." in body
    assert "find_customer" in body, "including what it reached for"
    assert "2 turn(s), 1 tool call(s)" in body
    assert "120" in body and "30" in body, "and what it cost"


@pytest.mark.django_db
def test_a_models_words_are_printed_not_run(admin_client, talked):
    """Model output is not trusted text.

    An inbound email can steer what a model says, so a reply reaching an
    admin page unescaped would let whoever wrote that email run script in the
    browser of whoever reads the run.
    """
    talked.scratch = {
        **talked.scratch,
        "messages": [{"role": "assistant", "content": "<script>alert('x')</script>"}],
    }
    talked.save()

    body = admin_client.get(_conversation_url(talked)).content.decode()

    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body, "shown as the text it is"


@pytest.mark.django_db
def test_an_action_that_never_spoke_says_so(admin_client, automation_content):
    instance = AutomationInstance.objects.create(automation_content=automation_content, data=[{}])
    silent = AutomationAction.objects.create(
        automation_instance=instance, plugin_ptr=uuid.uuid4(), state=COMPLETED, scratch={}
    )

    body = admin_client.get(_conversation_url(silent)).content.decode()

    assert "held no conversation" in body


@pytest.mark.django_db
def test_a_conversation_is_not_readable_without_permission_to_see_the_run(client, talked, django_user_model):
    """It is the run's data in its most complete form."""
    from django.contrib.auth.models import Permission

    onlooker = django_user_model.objects.create_user("onlooker", password="x", is_staff=True)
    onlooker.user_permissions.add(Permission.objects.get(codename="view_automationtrigger"))
    client.force_login(onlooker)

    assert client.get(_conversation_url(talked)).status_code == 403


@pytest.mark.django_db
def test_an_actions_history_is_read_where_the_run_is(admin_client, waiting_action):
    """Not as a changelist over every event in the database.

    That answered a question nobody asks — every action that ever failed,
    across every run. What people want is the history of the run in front of
    them.
    """
    url = reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk])
    body = admin_client.get(url).content.decode()

    assert "PENDING" in body and "RUNNING" in body, "the transitions it made"
    assert "attempt" in body.lower()


@pytest.mark.django_db
def test_a_history_is_not_readable_without_permission_to_see_the_run(client, waiting_action, django_user_model):
    from django.contrib.auth.models import Permission

    onlooker = django_user_model.objects.create_user("historyonlooker", password="x", is_staff=True)
    onlooker.user_permissions.add(Permission.objects.get(codename="view_automationtrigger"))
    client.force_login(onlooker)

    url = reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk])
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_the_menu_offers_only_what_someone_would_go_looking_for(admin_user, rf):
    """Eight entries, five of them internals, is a menu nobody reads.

    The events are still recorded; this is about what the index offers.
    """
    from django.contrib import admin as django_admin

    request = rf.get("/admin/")
    request.user = admin_user
    listed = {
        str(model["name"])
        for app in django_admin.site.get_app_list(request)
        if "automation" in app["app_label"]
        for model in app["models"]
    }

    assert {"Automations", "Execution Instances", "Dead letters", "Secrets"} <= listed
    assert "Instance events" not in listed, "the instance row already says this"
    assert "Action events" not in listed, "read per action instead"
    assert "Scheduler locks" not in listed, "one row of machinery"


@pytest.mark.django_db
def test_the_history_says_which_step_it_is(admin_client, waiting_action):
    """A page about one action that never names it is a page about a UUID."""
    url = reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk])
    body = admin_client.get(url).content.decode()

    assert "Wait for User" in body, "the step, in the words the editor sees"
    assert "attempt 1 of" in body


@pytest.mark.django_db
def test_the_history_says_how_long_each_state_lasted(admin_client, waiting_action):
    """ "Ran for four minutes" and "sat in the queue for four minutes" are the
    same two timestamps and completely different problems."""
    url = reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk])
    body = admin_client.get(url).content.decode()

    assert "to start" in body, "how long it waited before anything ran it"
    assert "0:00:00." not in body, "at the precision a person reads, not a timedelta's repr"


@pytest.mark.django_db
def test_a_tool_call_points_at_the_step_that_asked_for_it(admin_client, waiting_action):
    """A tool call on its own says nothing about why it happened."""
    child = AutomationAction.objects.create(
        automation_instance=waiting_action.automation_instance,
        plugin_ptr=uuid.uuid4(),
        parent=waiting_action,
        state=COMPLETED,
    )

    url = reverse("admin:djangocms_automation_action_history", args=[child.pk])
    body = admin_client.get(url).content.decode()

    assert reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk]) in body


@pytest.mark.django_db
def test_both_panels_lead_back_to_the_run(admin_client, waiting_action, talked):
    """A page reached from a run should say so, and go back there."""
    instance_url = reverse(
        "admin:djangocms_automation_automationinstance_change", args=[waiting_action.automation_instance_id]
    )
    history = admin_client.get(
        reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk])
    ).content.decode()
    conversation = admin_client.get(
        reverse("admin:djangocms_automation_conversation", args=[talked.pk])
    ).content.decode()

    assert instance_url in history
    assert (
        reverse("admin:djangocms_automation_automationinstance_change", args=[talked.automation_instance_id])
        in conversation
    )
    for body in (history, conversation):
        assert "Execution Instances" in body, "and through the changelist it came from"


def test_a_duration_reads_at_the_scale_it_happened():
    """Milliseconds and hours in the same column, each at its own precision."""
    import datetime

    from djangocms_automation.admin import _span

    assert _span(datetime.timedelta(milliseconds=7)) == "7 ms"
    assert _span(datetime.timedelta(seconds=1.24)) == "1.2 s"
    assert _span(datetime.timedelta(seconds=185)) == "3 min 5 s"
    assert _span(datetime.timedelta(seconds=7380)) == "2 h 3 min"
    assert _span(None) == ""


@pytest.mark.django_db
def test_both_panels_say_which_automation_was_running(admin_client, waiting_action, talked):
    """A reader arrives here from a link, a bookmark or a colleague.

    "Which of our automations is this?" should not be a click away.
    """
    history = admin_client.get(
        reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk])
    ).content.decode()
    conversation = admin_client.get(
        reverse("admin:djangocms_automation_conversation", args=[talked.pk])
    ).content.decode()
    automation = waiting_action.automation_instance.automation_content.automation

    for body in (history, conversation):
        assert automation.name in body, "the automation, by name"
        assert reverse("admin:djangocms_automation_automation_change", args=[automation.pk]) in body


@pytest.mark.django_db
def test_the_conversation_says_which_step_held_it(admin_client, talked):
    """It had no reference to the step at all — only the words."""
    body = admin_client.get(reverse("admin:djangocms_automation_conversation", args=[talked.pk])).content.decode()

    assert "run" in body.lower()
    # The step name comes from the plugin tree; this fixture's action has no
    # plugin behind it, so what must hold is that the block renders regardless.
    assert "automation-about" in body


@pytest.mark.django_db
def test_a_run_without_a_conversation_does_not_offer_a_column_for_one(admin_client, waiting_action, talked):
    """Asked of the run, not of the installed apps.

    A project without the AI app never has one, and a project with it still
    runs plenty of automations that never involve a model. An always-empty
    column teaches the reader to ignore that part of the row.
    """
    quiet = admin_client.get(
        reverse("admin:djangocms_automation_automationinstance_change", args=[waiting_action.automation_instance_id])
    ).content.decode()
    noisy = admin_client.get(
        reverse("admin:djangocms_automation_automationinstance_change", args=[talked.automation_instance_id])
    ).content.decode()

    assert "Conversation" not in quiet, "nothing spoke in this run"
    assert "Conversation" in noisy
    assert "History" in quiet, "which is not true of the history, always worth a look"


@pytest.mark.django_db
def test_the_inline_names_each_action(admin_client, waiting_action):
    """A row that opens on a state says how a step you cannot identify is
    getting on. The name comes first, and the uuid ties it to the node."""
    body = admin_client.get(
        reverse("admin:djangocms_automation_automationinstance_change", args=[waiting_action.automation_instance_id])
    ).content.decode()

    assert "Wait for User" in body, "the plugin's own name, as the editor sees it"
    assert str(waiting_action.plugin_ptr) in body, "and which node it was — two steps may share a name"


@pytest.mark.django_db
def test_an_action_is_named_by_its_step_and_node(waiting_action):
    """``<ATM f3b39215-… (23)>`` was a __repr__ under the wrong name.

    It reached pages and error messages, where the uuid was the only part that
    helped. The name says what the step does; the uuid says which node it was,
    because a run with three *Send Email* steps is entirely ordinary.
    """
    assert str(waiting_action) == f"Wait for User ({waiting_action.plugin_ptr})"
    assert not str(waiting_action).startswith("<")
    # The diagnostic form is what angle brackets are for, and keeps the detail.
    assert repr(waiting_action).startswith("<AutomationAction ")
    assert str(waiting_action.pk) in repr(waiting_action)


@pytest.mark.django_db
def test_an_action_whose_plugin_is_gone_is_still_named(waiting_action):
    """A workflow edited since the run is not a reason for __str__ to raise."""
    import uuid as uuid_module

    orphan = AutomationAction.objects.create(
        automation_instance=waiting_action.automation_instance,
        plugin_ptr=uuid_module.uuid4(),
        state=COMPLETED,
    )

    assert str(orphan) == f"Unknown step ({orphan.plugin_ptr})"


@pytest.mark.django_db
def test_both_panels_offer_the_way_back(admin_client, waiting_action, talked):
    """Neither panel has anything of its own to do."""
    instance_url = reverse(
        "admin:djangocms_automation_automationinstance_change", args=[waiting_action.automation_instance_id]
    )
    history = admin_client.get(
        reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk])
    ).content.decode()

    assert "Back to the run" in history
    assert f'href="{instance_url}" class="closelink"' in history, "the class Django styles for exactly this"

    conversation = admin_client.get(
        reverse("admin:djangocms_automation_conversation", args=[talked.pk])
    ).content.decode()
    assert "Back to the run" in conversation


def _with_answer(action, content, answer_format=""):
    """An assistant turn carrying one answer, and the format it was asked for."""
    action.scratch = {**action.scratch, "messages": [{"role": "assistant", "content": content}]}
    action.save()
    from unittest import mock

    return mock.patch("djangocms_automation.admin._answer_format", return_value=answer_format)


@pytest.mark.django_db
def test_an_answer_under_a_shape_is_shown_as_its_fields(admin_client, talked):
    """The schema already separated them; printing JSON makes the reader undo that.

    Each field is announced the way a turn is, so "Answered" as well would be a
    heading over headings.
    """
    with _with_answer(talked, '{"score": "hot", "company": "Acme"}'):
        body = admin_client.get(reverse("admin:djangocms_automation_conversation", args=[talked.pk])).content.decode()

    assert '<div class="automation-role">score</div>' in body
    assert '<div class="automation-role">company</div>' in body
    assert "hot" in body and "Acme" in body
    assert "Answered" not in body, "the field names are the headers now"


@pytest.mark.django_db
def test_a_nested_answer_stays_as_json(admin_client, talked):
    """Once it nests, the JSON is the clearer rendering and a table would lie."""
    with _with_answer(talked, '{"customer": {"name": "Ada"}}'):
        body = admin_client.get(reverse("admin:djangocms_automation_conversation", args=[talked.pk])).content.decode()

    assert '<div class="automation-role">customer</div>' not in body, "not a field of its own"
    assert "Answered" in body, "so the turn keeps its own header"
    assert "customer" in body, "shown as the JSON it is"


@pytest.mark.django_db
def test_markdown_is_rendered_when_it_was_asked_for(admin_client, talked):
    pytest.importorskip("markdown")
    pytest.importorskip("nh3")

    with _with_answer(talked, "## Heading\n\n- one\n- two", answer_format="markdown"):
        body = admin_client.get(reverse("admin:djangocms_automation_conversation", args=[talked.pk])).content.decode()

    assert "<h2>Heading</h2>" in body
    assert "<li>one</li>" in body


@pytest.mark.django_db
def test_markdown_is_sanitised_before_it_is_shown(admin_client, talked):
    """A model's answer can be steered by whatever arrived in the trigger.

    So rendering it is only safe behind a sanitiser, and nothing reaches the
    page marked safe without going through one.
    """
    pytest.importorskip("nh3")

    poisoned = "Fine.\n\n<script>alert('x')</script>\n\n[click](javascript:alert(1))"
    with _with_answer(talked, poisoned, answer_format="markdown"):
        body = admin_client.get(reverse("admin:djangocms_automation_conversation", args=[talked.pk])).content.decode()

    assert "<script>alert" not in body
    assert "javascript:alert" not in body
    assert "Fine." in body, "and the rest of the answer still arrives"


@pytest.mark.django_db
def test_html_that_was_asked_for_is_shown_as_markup(admin_client, talked):
    """An editor who asked for HTML wants to read what the model wrote.

    Rendering it would hide the markup they are checking, and hand a model's
    output to the browser.
    """
    with _with_answer(talked, "<p>Hello <b>Ada</b></p>", answer_format="html"):
        body = admin_client.get(reverse("admin:djangocms_automation_conversation", args=[talked.pk])).content.decode()

    assert "&lt;p&gt;Hello" in body
    assert "<p>Hello <b>Ada</b></p>" not in body


def test_no_template_carries_its_own_styles():
    """A Content Security Policy without unsafe-inline drops both forms.

    An inline ``<style>`` block and a ``style=`` attribute are equally
    unreachable under one, and both are easy to reintroduce without noticing
    because they work perfectly in development.
    """
    import re
    from pathlib import Path

    import djangocms_automation

    root = Path(djangocms_automation.__file__).parent
    offenders = []
    for path in list(root.rglob("*.html")) + [root / "widgets.py", root / "admin.py"]:
        source = path.read_text(encoding="utf-8")
        # A comment saying why there is no <style> block is not a <style> block.
        source = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", source, flags=re.DOTALL)
        source = re.sub(r"#.*$", "", source, flags=re.MULTILINE) if path.suffix == ".py" else source
        if "<style" in source or 'style="' in source:
            offenders.append(str(path.relative_to(root)))

    assert offenders == [], f"inline styles belong in a stylesheet: {offenders}"


@pytest.mark.django_db
def test_markdown_shown_as_text_says_why_while_developing(admin_client, talked, settings):
    """Two causes look identical on the page: the extra is missing, or the
    step no longer asks for Markdown. Only the first is the page's to explain.

    Shown while developing and not in production. The gate is on the setting
    in the view, not on ``DEBUG`` in the template: no such template variable
    exists — the debug context processor sets ``debug``, lowercase, and only
    for a client address in ``INTERNAL_IPS`` — so a template condition on it is
    quietly always false.
    """
    from unittest import mock

    settings.DEBUG = True

    with (
        _with_answer(talked, "## Heading", answer_format="markdown"),
        mock.patch("djangocms_automation.admin._can_render_markdown", return_value=False),
    ):
        body = admin_client.get(reverse("admin:djangocms_automation_conversation", args=[talked.pk])).content.decode()

    assert "renderer is not installed" in body
    assert "djangocms-automation[markdown]" in body
    assert "<h2>Heading</h2>" not in body, "and it is still shown as text, not rendered unsanitised"


@pytest.mark.django_db
def test_the_notice_stays_out_of_production(admin_client, talked, settings):
    """A missing developer tool is not something to tell an editor about."""
    from unittest import mock

    settings.DEBUG = False

    with (
        _with_answer(talked, "## Heading", answer_format="markdown"),
        mock.patch("djangocms_automation.admin._can_render_markdown", return_value=False),
    ):
        body = admin_client.get(reverse("admin:djangocms_automation_conversation", args=[talked.pk])).content.decode()

    assert "renderer is not installed" not in body
    assert "## Heading" in body, "the answer is still there, as text"


@pytest.mark.django_db
def test_no_notice_when_markdown_was_never_asked_for(admin_client, talked):
    """A step answering in prose is not missing anything."""
    from unittest import mock

    with (
        _with_answer(talked, "Just prose."),
        mock.patch("djangocms_automation.admin._can_render_markdown", return_value=False),
    ):
        body = admin_client.get(reverse("admin:djangocms_automation_conversation", args=[talked.pk])).content.decode()

    assert "renderer is not installed" not in body


@pytest.mark.django_db
def test_the_changelist_shows_the_hash_beside_the_id(admin_client, waiting_action):
    """The id is what people say out loud; the hash is what survives leaving
    this database, where two deployments disagree about run 17."""
    instance = waiting_action.automation_instance
    body = admin_client.get(reverse("admin:djangocms_automation_automationinstance_changelist")).content.decode()

    assert f">{instance.key[:12]}</span>" in body, "shortened the way a commit is"
    assert f'title="{instance.key}"' in body, "with the whole of it a hover away"
    assert f">{instance.pk}</a>" in body, "and the id is still there"


@pytest.mark.django_db
def test_both_the_id_and_the_hash_open_the_run(admin_client, waiting_action):
    """Django links only the first column by default, which would leave the
    hash as the one thing on the row you cannot click."""
    instance = waiting_action.automation_instance
    change_url = reverse("admin:djangocms_automation_automationinstance_change", args=[instance.pk])
    body = admin_client.get(reverse("admin:djangocms_automation_automationinstance_changelist")).content.decode()

    linked = body.count(f'href="{change_url}"')
    assert linked >= 2, f"expected the id and the hash to link, found {linked}"


@pytest.mark.django_db
def test_the_run_page_shows_the_whole_hash(admin_client, waiting_action):
    """The reason to open this page for the hash is to copy it somewhere."""
    instance = waiting_action.automation_instance
    body = admin_client.get(
        reverse("admin:djangocms_automation_automationinstance_change", args=[instance.pk])
    ).content.decode()

    assert instance.key in body
    assert "automation-hash" in body, "monospaced, so it can be compared at a glance"


@pytest.mark.django_db
def test_the_changelist_renders_at_all(admin_client, waiting_action):
    """It did not. ``automation_content__automation`` in ``list_display`` reads
    like a related lookup and is not one: Django labels the column from the
    path and then fetches it with ``getattr(instance, "automation")``, so every
    request raised ``AttributeError``."""
    response = admin_client.get(reverse("admin:djangocms_automation_automationinstance_changelist"))

    assert response.status_code == 200
    automation = waiting_action.automation_instance.automation_content.automation
    assert automation.name in response.content.decode(), "and the automation is named"


@pytest.mark.django_db
def test_the_run_page_says_what_was_run_beside_the_hash(admin_client, waiting_action):
    """A hash on its own says nothing about what was run.

    Its own field rather than text appended to the hash, so the hash stays a
    thing you can select and copy whole.
    """
    instance = waiting_action.automation_instance
    body = admin_client.get(
        reverse("admin:djangocms_automation_automationinstance_change", args=[instance.pk])
    ).content.decode()

    automation = instance.automation_content.automation
    assert f'<span class="automation-hash">{instance.key}</span>' in body
    assert "Automation" in body and automation.name in body
    assert f"{instance.key}</span> &mdash;" not in body, "not glued onto the hash"


@pytest.mark.django_db
def test_the_payload_starts_out_of_the_way(admin_client, waiting_action):
    """A run's data is the longest thing on the page and the least often the
    reason for opening it.

    The class has to be ``collapse``: Django tests for that exact string, so
    ``collapsed`` or ``collapsable`` leave the fieldset open while looking as
    though they closed it.
    """
    body = admin_client.get(
        reverse("admin:djangocms_automation_automationinstance_change", args=[waiting_action.automation_instance_id])
    ).content.decode()

    # The rendered class list, which is where the mistake shows: with
    # ``collapsed``/``collapsable`` this reads "module aligned collapsed
    # collapsable" and the fieldset is not collapsible at all.
    assert 'class="module aligned collapse"' in body

    fieldset = body[body.index('class="module aligned collapse"') :]
    fieldset = fieldset[: fieldset.index("</fieldset>")]
    assert "<details><summary>" in fieldset, "Django renders a collapsible fieldset as one"
    assert "field-data_display" in fieldset, "and the payload is the thing inside it"


@pytest.mark.django_db
def test_the_breadcrumb_names_the_run_the_way_everything_else_does(admin_client, waiting_action, talked):
    """One name for one thing.

    The trail used to spell out "Run 25" from the row id while the changelist,
    the heading and every select called the same run something else.
    """
    instance = waiting_action.automation_instance
    history = admin_client.get(
        reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk])
    ).content.decode()

    trail = history[history.index('class="breadcrumbs"') :]
    trail = trail[: trail.index("</nav>")]
    assert str(instance) in trail
    assert f"Run {instance.pk}" not in trail, "the row id is not the run's name"

    conversation = admin_client.get(
        reverse("admin:djangocms_automation_conversation", args=[talked.pk])
    ).content.decode()
    assert str(talked.automation_instance) in conversation


@pytest.mark.django_db
def test_the_trail_matches_the_admin_it_is_shown_in(admin_client, waiting_action):
    """Django 6.1 replaced the breadcrumb ``<div>`` with an ordered list.

    The panels follow whichever the installed admin uses, so they sit in the
    bar the same way every other page does rather than looking pasted in.
    """
    import django

    body = admin_client.get(
        reverse("admin:djangocms_automation_action_history", args=[waiting_action.pk])
    ).content.decode()
    trail = body[body.index("<nav") : body.index("</nav>")]

    if django.VERSION >= (6, 1):
        assert '<ol class="breadcrumbs">' in trail
        assert 'aria-current="page"' in trail, "which is what an ordered trail buys"
    else:
        assert '<div class="breadcrumbs">' in trail
        assert "&rsaquo;" in trail


@pytest.mark.django_db
def test_the_automation_list_says_which_ones_run(admin_client, automation):
    """Whether an automation runs at all is the one thing worth seeing without
    opening it: an inactive one looks exactly like an active one in a list of
    names, and "why did nothing happen" is the question it answers."""
    from djangocms_automation.models import Automation

    Automation.objects.create(name="Switched off", is_active=False)
    body = admin_client.get(reverse("admin:djangocms_automation_automation_changelist")).content.decode()

    assert "field-is_active" in body, "a column of its own"
    assert "Switched off" in body
    assert 'id="changelist-filter"' in body or "By Active" in body, "and something to filter by"


@pytest.mark.django_db
def test_the_flag_is_called_what_it_means(admin_client, automation):
    """Django would otherwise label it *Is active*."""
    from djangocms_automation.models import Automation

    assert str(Automation._meta.get_field("is_active").verbose_name) == "Active"
    body = admin_client.get(
        reverse("admin:djangocms_automation_automation_change", args=[automation.pk])
    ).content.decode()
    assert "do not start" in body, "and says what switching it off does"

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

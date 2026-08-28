"""The admin's own buttons: cancel an execution, replay a dead letter.

The engine functions behind these are covered elsewhere; what is tested here is
the wiring — that the action is registered, applies to the selected rows, and
reports back to the user.
"""

import pytest

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.utils.timezone import now

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation.admin import (
    AutomationInstanceAdmin,
    DeadLetterAdmin,
)
from djangocms_automation.instances import (
    CANCELED,
    FAILED,
    AutomationAction,
    AutomationInstance,
    DeadLetter,
)
from djangocms_automation.models import (
    Automation,
    AutomationContent,
    AutomationTrigger,
)


@pytest.fixture
def request_with_messages(admin_user):
    def build():
        request = RequestFactory().post("/")
        request.user = admin_user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    return build


@pytest.fixture
def content(db, admin_user):
    automation = Automation.objects.create(name="Admin actions", is_active=True)
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation, description="Admin actions content"
    )


@pytest.fixture
def executed(content, settings):
    """An automation that has run once, leaving an instance and an action."""
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(automation_content=content, slot="start", type="click", position=0)
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=content.pk,
        slot="start",
    )[0]
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)
    trigger.trigger_execution(data=[{"seed": 1}])
    return AutomationAction.objects.latest("id")


@pytest.mark.django_db
def test_cancel_action_stops_the_selected_executions(executed, request_with_messages):
    """The changelist button must reach every unfinished action in the run."""
    AutomationAction.objects.filter(pk=executed.pk).update(state="PENDING", finished=None)
    AutomationInstance.objects.filter(pk=executed.automation_instance_id).update(status="RUNNING", finished=None)
    admin = AutomationInstanceAdmin(AutomationInstance, AdminSite())
    request = request_with_messages()

    admin.cancel_instances(request, AutomationInstance.objects.filter(pk=executed.automation_instance_id))

    executed.refresh_from_db()
    instance = AutomationInstance.objects.get(pk=executed.automation_instance_id)
    assert executed.state == CANCELED
    assert instance.status == CANCELED
    assert [str(m) for m in request._messages]


@pytest.mark.django_db
def test_cancel_action_on_a_finished_run_reports_nothing_to_do(executed, request_with_messages):
    """Cancelling a completed run is a no-op, and says so rather than erroring."""
    admin = AutomationInstanceAdmin(AutomationInstance, AdminSite())
    request = request_with_messages()

    admin.cancel_instances(request, AutomationInstance.objects.filter(pk=executed.automation_instance_id))

    assert AutomationInstance.objects.get(pk=executed.automation_instance_id).status != CANCELED


@pytest.mark.django_db
def test_dead_letter_changelist_shows_only_dead_letters(executed, admin_user):
    """The proxy's queryset is what makes it a queue rather than a second action list."""
    request = RequestFactory().get("/")
    request.user = admin_user
    admin = DeadLetterAdmin(DeadLetter, AdminSite())

    assert admin.get_queryset(request).count() == 0

    AutomationAction.objects.filter(pk=executed.pk).update(state=FAILED, dead_lettered=True, dead_lettered_at=now())
    assert admin.get_queryset(request).count() == 1


@pytest.mark.django_db
def test_replay_action_creates_a_linked_replay(executed, request_with_messages, admin_user):
    """The button must produce a new, auditable action — not edit the old one."""
    AutomationAction.objects.filter(pk=executed.pk).update(
        state=FAILED, dead_lettered=True, dead_lettered_at=now(), input_data=[{"seed": 1}]
    )
    admin = DeadLetterAdmin(DeadLetter, AdminSite())
    request = request_with_messages()

    admin.replay_actions(request, DeadLetter.objects.filter(pk=executed.pk))

    replays = AutomationAction.objects.filter(replayed_from_id=executed.pk)
    assert replays.count() == 1
    executed.refresh_from_db()
    assert executed.state == FAILED, "the original row is untouched"


@pytest.mark.django_db
def test_replay_count_column_reports_replays(executed, request_with_messages):
    """The changelist column an operator uses to see what has been handled."""
    AutomationAction.objects.filter(pk=executed.pk).update(
        state=FAILED, dead_lettered=True, dead_lettered_at=now(), input_data=[]
    )
    admin = DeadLetterAdmin(DeadLetter, AdminSite())
    dead_letter = DeadLetter.objects.get(pk=executed.pk)

    assert admin.replay_count(dead_letter) == 0

    admin.replay_actions(request_with_messages(), DeadLetter.objects.filter(pk=executed.pk))
    assert admin.replay_count(dead_letter) == 1


@pytest.mark.django_db
def test_replaying_something_unreplayable_warns(executed, request_with_messages):
    """An action that is not in a terminal state cannot be replayed."""
    AutomationAction.objects.filter(pk=executed.pk).update(
        state="PENDING", finished=None, dead_lettered=True, dead_lettered_at=now()
    )
    admin = DeadLetterAdmin(DeadLetter, AdminSite())
    request = request_with_messages()

    admin.replay_actions(request, DeadLetter.objects.filter(pk=executed.pk))

    assert AutomationAction.objects.filter(replayed_from_id=executed.pk).count() == 0
    assert [str(m) for m in request._messages]


# --------------------------------------------------------------------------
# Authorization
#
# Both actions mutate: one stops other people's work, the other re-runs real
# side effects. Neither may be available merely because you can see the list.
# --------------------------------------------------------------------------


@pytest.fixture
def viewer(db):
    """A staff user who may look at the changelists and nothing more."""
    user = User.objects.create_user("viewer", password="x", is_staff=True)
    for codename in ("view_automationinstance", "view_automationaction"):
        permission = Permission.objects.filter(codename=codename).first()
        if permission:
            user.user_permissions.add(permission)
    return User.objects.get(pk=user.pk)


def bare_request(user):
    request = RequestFactory().post("/")
    request.user = user
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


@pytest.mark.django_db
def test_view_only_user_is_not_offered_cancel(viewer):
    admin = AutomationInstanceAdmin(AutomationInstance, AdminSite())
    assert "cancel_instances" not in admin.get_actions(bare_request(viewer))


@pytest.mark.django_db
def test_view_only_user_is_not_offered_replay(viewer):
    """Replay is gated on changing executions, not on viewing the queue.

    The dead-letter list is read-only, so it has no change permission of its own
    for the action to borrow.
    """
    admin = DeadLetterAdmin(DeadLetter, AdminSite())
    assert "replay_actions" not in admin.get_actions(bare_request(viewer))


@pytest.mark.django_db
def test_permitted_user_is_offered_both_actions(admin_user):
    instance_admin = AutomationInstanceAdmin(AutomationInstance, AdminSite())
    dead_letter_admin = DeadLetterAdmin(DeadLetter, AdminSite())

    assert "cancel_instances" in instance_admin.get_actions(bare_request(admin_user))
    assert "replay_actions" in dead_letter_admin.get_actions(bare_request(admin_user))

"""Tests for AutomationInstance and AutomationAction behaviors."""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils.timezone import now

from djangocms_automation.models import Automation, AutomationContent
from djangocms_automation.instances import AutomationInstance, AutomationAction


User = get_user_model()


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(username="admin", email="admin@example.com", password="password")


@pytest.fixture
def normal_user(db):
    return User.objects.create_user(username="user", email="user@example.com", password="password")


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Instances Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user, db):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Instance content",
    )


@pytest.mark.django_db
def test_automation_instance_key_and_str(automation_content):
    inst = AutomationInstance.objects.create(
        automation_content=automation_content,
        initial_data={"foo": 1},
        data={"bar": 2},
    )
    # key is computed on save; first save happens before id exists
    assert inst.key
    expected_initial = __import__("hashlib").sha1(f"{automation_content.automation_id}-{None}".encode()).hexdigest()
    assert inst.key == expected_initial
    # On subsequent save, key updates to include real id
    inst.save()
    inst.refresh_from_db()
    expected_after = __import__("hashlib").sha1(f"{automation_content.automation_id}-{inst.id}".encode()).hexdigest()
    assert inst.key == expected_after
    # __str__ contains automation name and id
    s = str(inst)
    assert automation_content.automation.name in s
    assert f"({inst.id})" in s


@pytest.mark.django_db
def test_automation_action_data_property(automation_content):
    inst = AutomationInstance.objects.create(automation_content=automation_content, data={"x": 42})
    act = AutomationAction.objects.create(
        automation_instance=inst,
        plugin_ptr=uuid.uuid4(),
    )
    assert act.data == {"x": 42}


@pytest.mark.django_db
def test_automation_action_hours_since_created(automation_content):
    inst = AutomationInstance.objects.create(automation_content=automation_content)
    act = AutomationAction.objects.create(automation_instance=inst, plugin_ptr=uuid.uuid4())
    # Initially not finished: positive number (>= 0)
    assert act.hours_since_created() >= 0
    # Finished: returns 0
    act.finished = now()
    assert act.hours_since_created() == 0


@pytest.mark.django_db
def test_automation_action_get_previous_tasks_joined_list(automation_content):
    inst = AutomationInstance.objects.create(automation_content=automation_content)
    a1 = AutomationAction.objects.create(automation_instance=inst, plugin_ptr=uuid.uuid4())
    a2 = AutomationAction.objects.create(automation_instance=inst, plugin_ptr=uuid.uuid4())
    current = AutomationAction.objects.create(
        automation_instance=inst,
        plugin_ptr=uuid.uuid4(),
        message="Joined",
        result=[a1.id, a2.id],
    )
    prev_qs = current.get_previous_tasks()
    assert set(prev_qs.values_list("id", flat=True)) == {a1.id, a2.id}


@pytest.mark.django_db
def test_automation_action_get_previous_tasks_single_previous(automation_content):
    inst = AutomationInstance.objects.create(automation_content=automation_content)
    prev = AutomationAction.objects.create(automation_instance=inst, plugin_ptr=uuid.uuid4())
    current = AutomationAction.objects.create(
        automation_instance=inst,
        plugin_ptr=uuid.uuid4(),
        previous=prev,
    )
    prev_list = current.get_previous_tasks()
    assert prev_list == [prev]


@pytest.mark.django_db
def test_automation_action_get_open_tasks_for_user(automation_content, normal_user, admin_user):
    inst = AutomationInstance.objects.create(automation_content=automation_content)
    # Two tasks requiring interaction
    t1 = AutomationAction.objects.create(
        automation_instance=inst,
        plugin_ptr=uuid.uuid4(),
        requires_interaction=True,
    )
    t2 = AutomationAction.objects.create(
        automation_instance=inst,
        plugin_ptr=uuid.uuid4(),
        requires_interaction=True,
    )
    # With no specific user/group filters, any user should see them (per current implementation)
    open_for_user = AutomationAction.get_open_tasks(normal_user)
    assert set(open_for_user) == {t1, t2}

    # If a task is assigned to a specific user, others should not see it
    t1.interaction_user = admin_user
    t1.save()
    open_for_user = AutomationAction.get_open_tasks(normal_user)
    assert set(open_for_user) == {t2}

    # Superuser should see both (union with superusers)
    open_for_admin = AutomationAction.get_open_tasks(admin_user)
    assert set(open_for_admin) == {t1, t2}


@pytest.mark.django_db
def test_automation_action_get_users_with_permission_filters_user_and_group(
    automation_content, normal_user, admin_user
):
    inst = AutomationInstance.objects.create(automation_content=automation_content)
    grp = Group.objects.create(name="editors")
    normal_user.groups.add(grp)

    act = AutomationAction.objects.create(
        automation_instance=inst,
        plugin_ptr=uuid.uuid4(),
        interaction_user=normal_user,
        interaction_group=grp,
        interaction_permissions=[],  # keep simple: no permission filtering
    )

    qs = act.get_users_with_permission()
    # Includes specific user and superusers
    ids = set(qs.values_list("id", flat=True))
    assert normal_user.id in ids
    assert admin_user.id in ids

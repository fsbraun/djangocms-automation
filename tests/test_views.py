"""Tests for AutomationView context and placeholder handling."""

import pytest
from django.test import RequestFactory
from django.contrib.contenttypes.models import ContentType

from cms.models import Placeholder

from djangocms_automation.views import AutomationView
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture
def automation_content(admin_user, db):
    automation = Automation.objects.create(name="View Test", is_active=True)
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Automation content for view tests",
    )


@pytest.mark.django_db
def test_automation_view_creates_missing_placeholders_and_assigns(rf, automation_content):
    # Create two triggers with distinct slots
    t1 = AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="start",
        type="click",
        position=0,
    )
    t2 = AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="pause",
        type="click",
        position=1,
    )

    # Ensure no placeholders exist initially
    ct = ContentType.objects.get_for_model(AutomationContent)
    assert not Placeholder.objects.filter(content_type=ct, object_id=automation_content.pk).exists()

    # Call the view via as_view, passing the object as positional arg
    request = rf.get("/automation/")
    response = AutomationView.as_view()(request, automation_content)

    # TemplateResponse should have context_data
    context = response.context_data
    triggers = context["triggers"]

    # Verify triggers are in context and placeholders created/assigned
    assert len(triggers) == 2
    slots = {tr.slot for tr in triggers}
    assert slots == {"start", "pause"}

    # Placeholders are created for both slots
    placeholders = Placeholder.objects.filter(content_type=ct, object_id=automation_content.pk)
    assert placeholders.count() == 2
    ph_by_slot = {ph.slot: ph for ph in placeholders}

    # Each trigger has its placeholder set (compare by PK to avoid instance identity issues)
    for tr in triggers:
        assert tr.placeholder is not None
        assert tr.placeholder.pk == ph_by_slot[tr.slot].pk


@pytest.mark.django_db
def test_automation_view_uses_existing_placeholders_without_duplicate(rf, automation_content):
    # One trigger on 'start'
    AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="start",
        type="click",
        position=0,
    )

    # Pre-create placeholder for 'start'
    ct = ContentType.objects.get_for_model(AutomationContent)
    existing = Placeholder.objects.create(slot="start", content_type=ct, object_id=automation_content.pk)

    request = rf.get("/automation/")
    response = AutomationView.as_view()(request, automation_content)
    context = response.context_data
    triggers = context["triggers"]
    assert len(triggers) == 1

    # Still exactly one placeholder for 'start'
    placeholders = Placeholder.objects.filter(content_type=ct, object_id=automation_content.pk, slot="start")
    assert placeholders.count() == 1

    # Trigger placeholder is the pre-existing one
    assert triggers[0].placeholder.pk == existing.pk


@pytest.mark.django_db
def test_automation_view_with_no_triggers_returns_empty(rf, automation_content):
    # No triggers
    ct = ContentType.objects.get_for_model(AutomationContent)

    request = rf.get("/automation/")
    response = AutomationView.as_view()(request, automation_content)
    context = response.context_data

    # No triggers and no placeholders created
    assert context["triggers"] == []
    assert not Placeholder.objects.filter(content_type=ct, object_id=automation_content.pk).exists()

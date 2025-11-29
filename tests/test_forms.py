"""Tests for AutomationTriggerAdminForm and TriggerChoiceField."""

import pytest
from django import forms
from django.test import RequestFactory

from djangocms_automation.forms import AutomationTriggerAdminForm, TriggerChoiceField
from djangocms_automation.admin import AutomationTriggerAdmin
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
from djangocms_automation.triggers import trigger_registry


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture
def automation_content(admin_user, db):
    automation = Automation.objects.create(name="Form Test", is_active=True)
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Automation content for form tests",
    )


def test_trigger_choice_field_valid_value():
    field = TriggerChoiceField()
    assert field.valid_value("timer") is True
    assert field.valid_value("mail") is True
    assert field.valid_value("unknown") is False


def test_admin_form_widgets_hidden_and_choices():
    form = AutomationTriggerAdminForm()
    # Hidden widgets for automation_content and position
    assert isinstance(form.fields["automation_content"].widget, forms.HiddenInput)
    assert isinstance(form.fields["position"].widget, forms.HiddenInput)
    # Type choices come from registry
    assert form.fields["type"].choices == trigger_registry.get_choices()


@pytest.mark.django_db
def test_admin_form_clean_and_save_mail(rf, automation_content):
    # Prepare an unsaved instance with type 'timer'
    instance = AutomationTrigger(
        automation_content=automation_content,
        type="mail",
        slot="start",
        position=0,
    )

    # Build the admin-mixed form class to include timer config fields
    from django.contrib.admin.sites import AdminSite

    admin = AutomationTriggerAdmin(AutomationTrigger, AdminSite())
    # use GET so get_trigger returns based on obj.type
    form_class = admin.get_form(rf.get("/"), instance)

    # Provide data including the split datetime and other config
    data = {
        "automation_content": automation_content.pk,
        "type": "mail",
        "slot": "start",
        "position": 0,
        "recipient_filter": "*@example.com",
        "subject_contains": "Invoice",
        "status_filter": "sent",
    }

    form = form_class(data=data, instance=instance)
    assert form.is_valid(), form.errors

    saved = form.save(commit=True)
    # Config should be extracted and datetime converted to ISO string
    cfg = saved.config
    assert cfg["recipient_filter"] == "*@example.com"
    assert cfg["subject_contains"] == "Invoice"
    assert cfg["status_filter"] == "sent"

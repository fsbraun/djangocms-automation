"""Tests for AutomationTrigger admin form with dynamic config fields."""

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from djangocms_automation.admin import AutomationTriggerAdmin
from djangocms_automation.forms import AutomationTriggerAdminForm
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
from djangocms_automation.triggers import TimerTrigger, MailTrigger


User = get_user_model()


@pytest.fixture
def automation_content(db, admin_user):
    """Create a test automation content."""
    automation = Automation.objects.create(name="Test Automation", is_active=True)
    return AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Test automation content")


@pytest.fixture
def admin_user(db):
    """Create an admin user."""
    return User.objects.create_superuser(username="admin", email="admin@example.com", password="password")


@pytest.fixture
def request_factory():
    """Return a request factory."""
    return RequestFactory()


@pytest.fixture
def admin_site():
    """Return an admin site instance."""
    return AdminSite()


@pytest.mark.django_db
class TestAutomationTriggerAdminForm:
    """Test dynamic config field injection in AutomationTrigger admin form."""

    def test_form_has_basic_fields(self):
        """Form should have basic fields."""
        form = AutomationTriggerAdminForm()

        assert "automation_content" in form.fields
        assert "type" in form.fields
        assert "slot" in form.fields
        assert "position" in form.fields

    def test_admin_adds_timer_config_fields(self, automation_content, request_factory, admin_site):
        """Admin should add timer-specific config fields when type is timer."""
        trigger = AutomationTrigger.objects.create(
            automation_content=automation_content,
            type="timer",
            slot="test",
        )

        admin = AutomationTriggerAdmin(AutomationTrigger, admin_site)
        request = request_factory.get("/")
        form_class = admin.get_form(request, trigger)
        form = form_class(instance=trigger)

        # Check timer-specific fields are in the form
        assert "scheduled_at" in form.fields
        assert "timezone" in form.fields
        assert "recurrence_frequency" in form.fields
        assert "recurrence_interval" in form.fields
        assert "recurrence_end_date" in form.fields
        assert "recurrence_count" in form.fields

    def test_admin_adds_mail_config_fields(self, automation_content, request_factory, admin_site):
        """Admin should add mail-specific config fields when type is mail."""
        trigger = AutomationTrigger.objects.create(
            automation_content=automation_content,
            type="mail",
            slot="test",
        )

        admin = AutomationTriggerAdmin(AutomationTrigger, admin_site)
        request = request_factory.get("/")
        form_class = admin.get_form(request, trigger)
        form = form_class(instance=trigger)

        # Check mail-specific fields are in the form
        assert "recipient_filter" in form.fields
        assert "subject_contains" in form.fields
        assert "status_filter" in form.fields

    def test_admin_no_config_fields_for_click_trigger(self, automation_content, request_factory, admin_site):
        """Admin should not add config fields for triggers without fields."""
        trigger = AutomationTrigger.objects.create(
            automation_content=automation_content,
            type="click",
            slot="test",
        )

        admin = AutomationTriggerAdmin(AutomationTrigger, admin_site)
        request = request_factory.get("/")
        form_class = admin.get_form(request, trigger)
        form = form_class(instance=trigger)

        # Should only have base fields, no trigger-specific fields
        # (ClickTrigger has no declared fields)
        # Note: position field is included in readonly_fields
        base_fields = {"automation_content", "type", "slot", "position"}
        assert set(form.fields.keys()) == base_fields

    def test_admin_fieldsets_include_trigger_config(self, automation_content, request_factory, admin_site):
        """Admin fieldsets should include trigger configuration section."""
        trigger = AutomationTrigger.objects.create(
            automation_content=automation_content,
            type="timer",
            slot="test",
        )

        admin = AutomationTriggerAdmin(AutomationTrigger, admin_site)
        request = request_factory.get("/")
        fieldsets = admin.get_fieldsets(request, trigger)

        # Should have base fieldset and timer config fieldset
        assert len(fieldsets) == 2
        assert fieldsets[0][1]["fields"] == ("automation_content", "type", "slot", "position")

        # Second fieldset should be timer config
        timer_fields = fieldsets[1][1]["fields"]
        assert "scheduled_at" in timer_fields
        assert "timezone" in timer_fields

    def test_admin_get_trigger_with_obj(self, automation_content, request_factory, admin_site):
        """Admin get_trigger should return trigger class from object."""
        trigger = AutomationTrigger.objects.create(
            automation_content=automation_content,
            type="timer",
            slot="test",
        )

        admin = AutomationTriggerAdmin(AutomationTrigger, admin_site)
        request = request_factory.get("/")

        trigger_class, changed = admin.get_trigger(request, trigger)

        assert trigger_class == TimerTrigger
        assert changed is False

    def test_admin_get_trigger_with_get_param(self, request_factory, admin_site):
        """Admin get_trigger should return trigger class from GET parameter."""
        admin = AutomationTriggerAdmin(AutomationTrigger, admin_site)
        request = request_factory.get("/?type=mail")

        trigger_class, changed = admin.get_trigger(request, None)

        assert trigger_class == MailTrigger
        assert changed is False

    def test_admin_get_trigger_detects_type_change(self, automation_content, request_factory, admin_site):
        """Admin get_trigger should detect type changes in POST."""
        trigger = AutomationTrigger.objects.create(
            automation_content=automation_content,
            type="timer",
            slot="test",
        )

        admin = AutomationTriggerAdmin(AutomationTrigger, admin_site)
        request = request_factory.post("/", {"_trigger_type_change": "mail"})

        trigger_class, changed = admin.get_trigger(request, trigger)

        assert trigger_class == MailTrigger
        assert changed is True

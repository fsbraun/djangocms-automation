"""Tests for FormSubmissionTrigger and AutomationAction integration with djangocms_form_builder."""

import pytest

from django import forms
from django.contrib.contenttypes.models import ContentType

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation.models import (
    Automation,
    AutomationContent,
    AutomationTrigger,
)
from djangocms_automation.triggers import FormSubmissionTrigger


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Form Automation", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Automation triggered by form submission",
    )


@pytest.fixture
def automation_trigger(automation_content):
    """Create a form submission trigger."""
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content,
        slot="form_trigger",
        type="form_submission",
        position=0,
    )

    # Create placeholder and add a dummy action plugin
    content_type = ContentType.objects.get_for_model(AutomationContent)
    placeholder = Placeholder.objects.get_or_create(
        content_type=content_type,
        object_id=automation_content.pk,
        slot="form_trigger",
    )[0]

    add_plugin(
        placeholder=placeholder,
        plugin_type="DummyActionPlugin",
        language="en",
    )

    return trigger


class TestFormSubmissionTrigger:
    """Test FormSubmissionTrigger definition and schema."""

    def test_trigger_registered(self):
        from djangocms_automation.triggers import trigger_registry

        form_trigger = trigger_registry.get("form_submission")
        assert form_trigger is not None
        assert form_trigger.id == "form_submission"
        assert form_trigger.name == "Form Submission"

    def test_trigger_has_correct_icon(self):
        trigger = FormSubmissionTrigger()
        assert trigger.icon == "bi-ui-checks"

    def test_trigger_schema_is_empty(self):
        """FormSubmissionTrigger has no schema requirements."""
        trigger = FormSubmissionTrigger()
        assert trigger.data_schema == {}

    def test_trigger_validates_any_payload(self):
        """With empty schema, any payload should validate."""
        trigger = FormSubmissionTrigger()
        assert trigger.validate_payload({}) is True
        assert trigger.validate_payload({"foo": "bar"}) is True
        assert trigger.validate_payload({"nested": {"data": 123}}) is True


def _can_import_form_builder():
    """Check if djangocms_form_builder and its dependencies are available."""
    try:
        import djangocms_form_builder  # noqa: F401
        import djangocms_text  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _can_import_form_builder(), reason="djangocms_form_builder or djangocms_text not installed")
class TestFormBuilderAutomationAction:
    """Test AutomationAction integration with djangocms_form_builder."""

    def test_automation_action_class_exists(self):
        """Verify AutomationAction is defined when djangocms_form_builder is installed."""
        # Import should succeed if djangocms_form_builder is installed
        from djangocms_automation import triggers

        # The AutomationAction should be defined in the module
        assert hasattr(triggers, "AutomationAction")

    def test_automation_action_has_trigger_field(self):
        """AutomationAction should have a trigger ModelChoiceField."""
        from djangocms_automation.triggers import AutomationAction as FormAutomationAction

        # The trigger field is defined in the class body but as a form field descriptor
        # Check in base_fields which is set by the Form metaclass
        assert "trigger" in FormAutomationAction.base_fields
        assert isinstance(FormAutomationAction.base_fields["trigger"], forms.ModelChoiceField)

    def test_automation_action_has_entangled_config(self):
        """AutomationAction should have proper entangled fields configuration."""
        from djangocms_automation.triggers import AutomationAction as FormAutomationAction

        # Check Meta configuration
        assert hasattr(FormAutomationAction, "Meta")
        assert hasattr(FormAutomationAction.Meta, "entangled_fields")
        assert "action_parameters" in FormAutomationAction.Meta.entangled_fields
        assert "trigger" in FormAutomationAction.Meta.entangled_fields["action_parameters"]

    def test_automation_action_execute_method_exists(self):
        """AutomationAction should have an execute method."""
        from djangocms_automation.triggers import AutomationAction as FormAutomationAction

        assert hasattr(FormAutomationAction, "execute")
        assert callable(FormAutomationAction.execute)


@pytest.mark.django_db
class TestFormBuilderActionExecute:
    """Exercise the form action's execute() path end-to-end."""

    def test_execute_triggers_automation_with_serialized_data(self, settings, admin_user):
        from unittest import mock

        from django.contrib.contenttypes.models import ContentType
        from django.test import RequestFactory

        from cms.api import add_plugin
        from cms.models import Placeholder

        from djangocms_automation.instances import COMPLETED, AutomationAction
        from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
        from djangocms_automation.triggers import AutomationAction as FormAutomationAction

        settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}

        automation = Automation.objects.create(name="Form Exec", is_active=True)
        content = AutomationContent.objects.with_user(admin_user).create(
            automation=automation, description="form exec"
        )
        trigger = AutomationTrigger.objects.create(
            automation_content=content, slot="start", type="form_submission", position=0
        )
        placeholder = Placeholder.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(AutomationContent),
            object_id=content.pk,
            slot="start",
        )[0]
        add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)

        form = mock.Mock()
        form.cleaned_data = {"name": "Alice", "email": "alice@example.com"}
        request = RequestFactory().post("/")
        request.user = admin_user

        action = FormAutomationAction.__new__(FormAutomationAction)  # skip form __init__
        with mock.patch.object(FormAutomationAction, "get_parameter", return_value={"pk": automation.pk}):
            action.execute(form, request)

        instance = content.automationinstance_set.first()
        assert instance is not None
        assert instance.initial_data == [
            {"data": {"name": "Alice", "email": "alice@example.com"}, "user_id": admin_user.pk}
        ]
        run_action = AutomationAction.objects.get(automation_instance=instance)
        assert run_action.state == COMPLETED
        assert trigger.automation_content == content

    def test_execute_anonymous_user_records_null_user(self, settings, admin_user):
        from unittest import mock

        from django.contrib.auth.models import AnonymousUser
        from django.contrib.contenttypes.models import ContentType
        from django.test import RequestFactory

        from cms.models import Placeholder

        from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
        from djangocms_automation.triggers import AutomationAction as FormAutomationAction

        settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}

        automation = Automation.objects.create(name="Form Anon", is_active=True)
        content = AutomationContent.objects.with_user(admin_user).create(
            automation=automation, description="form anon"
        )
        AutomationTrigger.objects.create(automation_content=content, slot="start", type="form_submission", position=0)
        placeholder = Placeholder.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(AutomationContent),
            object_id=content.pk,
            slot="start",
        )[0]
        from cms.api import add_plugin

        add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)

        form = mock.Mock()
        form.cleaned_data = {"name": "Guest"}
        request = RequestFactory().post("/")
        request.user = AnonymousUser()

        action = FormAutomationAction.__new__(FormAutomationAction)
        with mock.patch.object(FormAutomationAction, "get_parameter", return_value={"pk": automation.pk}):
            action.execute(form, request)

        instance = content.automationinstance_set.first()
        assert instance.initial_data[0]["user_id"] is None

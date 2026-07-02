"""Tests for the CMS toolbar integration (cms_toolbars.AutomationToolbar)."""

from unittest import mock

import pytest

from django.test import RequestFactory

from djangocms_automation.cms_toolbars import AutomationToolbar
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Toolbar Test", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(
        automation=automation,
        description="Toolbar automation content",
    )


def _make_toolbar(user, obj):
    request = RequestFactory().get("/")
    request.user = user
    cms_toolbar = mock.MagicMock()
    cms_toolbar.get_object.return_value = obj
    return AutomationToolbar(request, cms_toolbar, is_current_app=True, app_path=None), cms_toolbar


@pytest.mark.django_db
def test_toolbar_populates_menu_with_triggers(admin_user, automation_content):
    AutomationTrigger.objects.create(automation_content=automation_content, slot="start", type="click")
    toolbar, cms_toolbar = _make_toolbar(admin_user, automation_content)

    toolbar.populate()

    cms_toolbar.get_or_create_menu.assert_called_once()
    menu = cms_toolbar.get_or_create_menu.return_value
    trigger_menu = menu.get_or_create_menu.return_value
    # Add Trigger + one trigger edit entry + the changelist entry
    labels = [str(call.args[0]) for call in trigger_menu.add_modal_item.call_args_list]
    assert "Add Trigger" in labels
    assert any("Start" in label for label in labels)  # the trigger itself
    assert "Triggers" in labels
    trigger_menu.add_break.assert_called()


@pytest.mark.django_db
def test_toolbar_skips_non_automation_objects(admin_user):
    toolbar, cms_toolbar = _make_toolbar(admin_user, object())

    toolbar.populate()

    cms_toolbar.get_or_create_menu.assert_not_called()


@pytest.mark.django_db
def test_toolbar_requires_view_permission(django_user_model, automation_content):
    plain_user = django_user_model.objects.create_user(username="plain", password="x")
    toolbar, cms_toolbar = _make_toolbar(plain_user, automation_content)

    toolbar.populate()

    cms_toolbar.get_or_create_menu.assert_not_called()


@pytest.mark.django_db
def test_toolbar_without_change_permission_disables_trigger_items(django_user_model, automation_content):
    from django.contrib.auth.models import Permission

    AutomationTrigger.objects.create(automation_content=automation_content, slot="start", type="click")
    user = django_user_model.objects.create_user(username="viewer", password="x")
    user.user_permissions.add(
        Permission.objects.get(codename="view_automationtrigger"),
        Permission.objects.get(codename="add_automationtrigger"),
    )
    user = django_user_model.objects.get(pk=user.pk)  # refresh perm cache
    toolbar, cms_toolbar = _make_toolbar(user, automation_content)

    toolbar.populate()

    menu = cms_toolbar.get_or_create_menu.return_value
    trigger_menu = menu.get_or_create_menu.return_value
    # The trigger entry is disabled (no change permission)...
    trigger_menu.add_disabled_item.assert_called_once()
    # ...while "Add Trigger" and the changelist modal are still offered.
    labels = [str(call.args[0]) for call in trigger_menu.add_modal_item.call_args_list]
    assert "Add Trigger" in labels

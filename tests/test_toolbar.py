"""Tests for the CMS toolbar integration (cms_toolbars.AutomationToolbar)."""

from unittest import mock

import pytest
from django.test import RequestFactory
from django.urls import reverse

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
    # Add Trigger, plus one entry per trigger. There is no changelist entry:
    # the submenu already lists them, and a link to the same list inside the
    # menu that is the list was one hop to nowhere.
    labels = [str(call.args[0]) for call in trigger_menu.add_modal_item.call_args_list]
    assert "Add Trigger" in labels
    assert any("Start" in label for label in labels)  # the trigger itself
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


@pytest.mark.django_db
def test_the_toolbar_offers_a_run(admin_user, automation_content):
    """The button for trying the thing you just built."""
    AutomationTrigger.objects.create(automation_content=automation_content, slot="start", type="click")
    toolbar, cms_toolbar = _make_toolbar(admin_user, automation_content)

    toolbar.populate()

    menu = cms_toolbar.get_or_create_menu.return_value
    labels = [str(call.args[0]) for call in menu.add_modal_item.call_args_list]
    assert "Run now…" in labels
    url = next(call.args[1] for call in menu.add_modal_item.call_args_list if str(call.args[0]) == "Run now…")
    assert url.endswith(f"?automation_content={automation_content.pk}")


@pytest.mark.django_db
def test_an_automation_with_no_trigger_says_so_rather_than_hiding_it(admin_user, automation_content):
    """An automation with no trigger has no entry point at all.

    Saying that in the menu is more use than an *Automation* menu that quietly
    lacks an item the editor read about somewhere.
    """
    toolbar, cms_toolbar = _make_toolbar(admin_user, automation_content)

    toolbar.populate()

    menu = cms_toolbar.get_or_create_menu.return_value
    disabled = [str(call.args[0]) for call in menu.add_disabled_item.call_args_list]
    assert "Run now…" in disabled
    assert not any(str(call.args[0]) == "Run now…" for call in menu.add_modal_item.call_args_list)


@pytest.mark.django_db
def test_running_is_not_offered_to_someone_who_may_not_start_one(automation_content, django_user_model):
    """Starting a run sends mail and writes records. It is not a view action."""
    onlooker = django_user_model.objects.create_user("onlooker", is_staff=True)
    from django.contrib.auth.models import Permission

    onlooker.user_permissions.add(Permission.objects.get(codename="view_automationtrigger"))
    onlooker = django_user_model.objects.get(pk=onlooker.pk)  # drop the permission cache

    AutomationTrigger.objects.create(automation_content=automation_content, slot="start", type="click")
    toolbar, cms_toolbar = _make_toolbar(onlooker, automation_content)

    toolbar.populate()

    menu = cms_toolbar.get_or_create_menu.return_value
    offered = [str(call.args[0]) for call in menu.add_modal_item.call_args_list]
    offered += [str(call.args[0]) for call in menu.add_disabled_item.call_args_list]
    assert "Run now…" not in offered


@pytest.mark.django_db
def test_the_toolbar_offers_this_automations_runs(admin_user, automation_content):
    """Filtered to this automation: the unfiltered list is every run of every
    automation on the site, and narrowing it is the first thing anyone would
    do with it."""
    toolbar, cms_toolbar = _make_toolbar(admin_user, automation_content)

    toolbar.populate()

    menu = cms_toolbar.get_or_create_menu.return_value
    labels = [str(call.args[0]) for call in menu.add_sideframe_item.call_args_list]
    assert "Past Runs" in labels
    url = next(call.args[1] for call in menu.add_sideframe_item.call_args_list if str(call.args[0]) == "Past Runs")
    assert url.endswith(f"?automation_content__automation__id__exact={automation_content.automation_id}")


@pytest.mark.django_db
def test_the_runs_open_beside_the_workflow_not_over_it(admin_user, automation_content):
    """A list of runs is read *against* the workflow — this step failed, which
    node is that — and a modal covers the thing you are comparing it with."""
    toolbar, cms_toolbar = _make_toolbar(admin_user, automation_content)

    toolbar.populate()

    menu = cms_toolbar.get_or_create_menu.return_value
    assert not any(str(call.args[0]) == "Runs" for call in menu.add_modal_item.call_args_list)


@pytest.mark.django_db
def test_runs_are_not_offered_to_someone_who_may_not_see_them(automation_content, django_user_model):
    """A run carries the data that flowed through it."""
    from django.contrib.auth.models import Permission

    onlooker = django_user_model.objects.create_user("runonlooker", is_staff=True)
    onlooker.user_permissions.add(Permission.objects.get(codename="view_automationtrigger"))
    onlooker = django_user_model.objects.get(pk=onlooker.pk)

    toolbar, cms_toolbar = _make_toolbar(onlooker, automation_content)
    toolbar.populate()

    menu = cms_toolbar.get_or_create_menu.return_value
    assert not any(str(call.args[0]) == "Runs" for call in menu.add_sideframe_item.call_args_list)


@pytest.mark.django_db
def test_a_trigger_can_be_deleted_from_the_modal(admin_client, automation_content):
    """Django hides Delete in a popup, and for a related-object lookup that is
    right: deleting would leave the opener holding a reference to something
    gone. A trigger opened from the toolbar is not that — the modal *is* how
    you edit one, so without this you have to go and find it in the admin."""
    trigger = AutomationTrigger.objects.create(automation_content=automation_content, slot="doomed", type="click")
    change = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])
    delete = reverse("admin:djangocms_automation_automationtrigger_delete", args=[trigger.pk])

    as_popup = admin_client.get(change + "?_popup=1").content.decode()
    assert 'class="deletelink"' in as_popup, "which is the class the CMS modal lifts into its footer"
    assert delete in as_popup

    # The confirmation has to carry the flag, or the POST comes back as a
    # redirect to a changelist that is not there to redirect to.
    confirmation = admin_client.get(delete + "?_popup=1").content.decode()
    assert 'name="_popup"' in confirmation

    response = admin_client.post(delete, {"post": "yes", "_popup": "1"})
    body = response.content.decode()
    # The popup response: a data island the admin's JS reads to close the
    # window. Not an inline ``opener.…`` call — that was the older shape.
    assert "django-admin-popup-response" in body
    assert "delete" in body, "and it says which action closed it"
    assert not AutomationTrigger.objects.filter(pk=trigger.pk).exists()


@pytest.mark.django_db
def test_a_direct_visit_still_gets_djangos_own_delete(admin_client, automation_content):
    """Outside a modal nothing changes: the ordinary link, the ordinary flow."""
    trigger = AutomationTrigger.objects.create(automation_content=automation_content, slot="ordinary", type="click")
    change = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])

    body = admin_client.get(change).content.decode()

    assert body.count('class="deletelink"') == 1, "one delete link, Django's own"
    assert "?_popup=1" not in body

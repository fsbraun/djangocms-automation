"""Adding automation plugins through the django CMS add-plugin endpoint."""

import pytest
from cms.models import Placeholder
from cms.plugin_pool import plugin_pool
from cms.utils.urlutils import admin_reverse
from django.contrib.contenttypes.models import ContentType

from djangocms_automation.cms_plugins import ActionPlugin
from djangocms_automation.data_editor import OutputsField
from djangocms_automation.models import Automation, AutomationContent

ACTIONS = sorted(p.__name__ for p in plugin_pool.get_all_plugins() if issubclass(p, ActionPlugin))


@pytest.fixture
def add_url(admin_user, db):
    automation = Automation.objects.create(name="Add plugin", is_active=True)
    content = AutomationContent.objects.with_user(admin_user).create(automation=automation, description="d")
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent), object_id=content.pk, slot="start"
    )[0]
    return admin_reverse("cms_placeholder_add_plugin") + (
        f"?placeholder_id={placeholder.pk}&plugin_language=en&plugin_position=1&cms_path=/&plugin_type="
    )


@pytest.mark.parametrize("name", ACTIONS)
def test_actions_are_added_through_their_form(name):
    """An action needs an intent, so django CMS must show its add form."""
    assert plugin_pool.get_plugin(name).show_add_form


@pytest.mark.parametrize("name", ACTIONS)
def test_incomplete_action_rerenders_its_form(admin_client, add_url, name):
    response = admin_client.post(add_url + name, {"outputs": "{}", "comment": ""})
    assert response.status_code == 200
    assert response.context["adminform"].form.errors


def test_outputs_field_renders_when_bound_to_parsed_value():
    """django CMS posts raw initial values for plugins without an add form."""
    assert OutputsField().bound_data({"a": {"field": "b"}}, None) == {"a": {"field": "b"}}
    assert OutputsField().bound_data('{"a": 1}', None) == {"a": 1}

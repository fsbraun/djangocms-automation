"""Tests for action data_form config persistence (ActionPlugin)."""

import pytest

from django import forms
from django.test import RequestFactory

from cms.plugin_pool import plugin_pool

from djangocms_automation.actions.mail import MailActionPluginModel


@pytest.fixture
def rf_request(admin_user):
    request = RequestFactory().get("/")
    request.user = admin_user
    return request


@pytest.mark.django_db
def test_data_form_fields_expression_and_template_modes(rf_request):
    plugin = plugin_pool.get_plugin("MailAction")(model=MailActionPluginModel, admin_site=None)
    fields = plugin.get_data_form_fields(rf_request, obj=None)

    assert set(fields) >= {"subject", "body", "recipient_email", "from_email"}
    # body is declared with a Textarea -> template mode
    assert isinstance(fields["body"].widget, forms.Textarea)
    assert isinstance(fields["subject"].widget, forms.TextInput)

    # Template validator accepts template text that is not a valid expression.
    fields["body"].validators[0]("Hello {{ user.name }}!")
    # Expression validator rejects free text.
    with pytest.raises(Exception):
        fields["subject"].validators[0]("not a valid expression!!")


@pytest.mark.django_db
def test_config_values_seed_initials(rf_request):
    model = MailActionPluginModel(config={"subject": "'Hi'", "body": "Hello {{ name }}"})
    plugin = plugin_pool.get_plugin("MailAction")(model=MailActionPluginModel, admin_site=None)
    fields = plugin.get_data_form_fields(rf_request, obj=model)
    assert fields["subject"].initial == "'Hi'"
    assert fields["body"].initial == "Hello {{ name }}"


@pytest.mark.django_db
def test_save_model_persists_config(rf_request):
    plugin = plugin_pool.get_plugin("MailAction")(model=MailActionPluginModel, admin_site=None)

    class FakeForm:
        cleaned_data = {
            "subject": "'Welcome'",
            "body": "Hi {{ name }}",
            "recipient_email": "email",
            "from_email": "",
            "comment": "irrelevant",
        }

    obj = MailActionPluginModel()

    # Avoid the full CMS admin save; call save_model and intercept the model save.
    saved = {}

    def fake_super_save(request, obj, form, change):
        saved["config"] = obj.config

    import unittest.mock as mock

    with mock.patch("cms.plugin_base.CMSPluginBase.save_model", side_effect=fake_super_save):
        plugin.save_model(rf_request, obj, FakeForm(), change=False)

    assert saved["config"] == {
        "subject": "'Welcome'",
        "body": "Hi {{ name }}",
        "recipient_email": "email",
        "from_email": "",
    }

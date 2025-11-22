"""Tests for the APIKey admin."""

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model

from djangocms_automation.admin import APIKeyAdmin, APIKeyAdminForm
from djangocms_automation.models import APIKey

User = get_user_model()


@pytest.fixture
def admin_site():
    """Create an admin site for testing."""
    return AdminSite()


@pytest.fixture
def api_key_admin(admin_site):
    """Create an APIKeyAdmin instance."""
    return APIKeyAdmin(APIKey, admin_site)


@pytest.fixture
def superuser():
    """Create a superuser for admin tests."""
    return User.objects.create_superuser(
        username="admin",
        email="admin@test.com",
        password="admin123",
    )


@pytest.mark.django_db
class TestAPIKeyAdminForm:
    """Test the APIKeyAdminForm."""

    def test_form_has_service_choices(self):
        """Test that the form has service choices."""
        form = APIKeyAdminForm()
        service_field = form.fields["service"]

        # Should have a Select widget with choices
        assert hasattr(service_field.widget, "choices")
        choices = list(service_field.widget.choices)
        assert len(choices) > 0

        # Check that choices include expected services
        choice_ids = [c[0] for c in choices]
        assert "openai" in choice_ids
        assert "github" in choice_ids

    def test_form_valid_data(self):
        """Test form with valid data."""
        form_data = {
            "name": "Test Key",
            "service": "openai",
            "api_key": "sk-test123",
            "description": "A test key",
            "is_active": True,
        }
        form = APIKeyAdminForm(data=form_data)

        assert form.is_valid()

    def test_form_missing_required_fields(self):
        """Test form with missing required fields."""
        form_data = {
            "description": "Missing required fields",
        }
        form = APIKeyAdminForm(data=form_data)

        assert not form.is_valid()
        assert "name" in form.errors
        assert "service" in form.errors
        assert "api_key" in form.errors

    def test_form_save(self):
        """Test saving a form."""
        form_data = {
            "name": "Form Test Key",
            "service": "github",
            "api_key": "ghp_formtest",
            "description": "Created via form",
            "is_active": True,
        }
        form = APIKeyAdminForm(data=form_data)

        assert form.is_valid()
        api_key = form.save()

        assert api_key.id is not None
        assert api_key.name == "Form Test Key"
        assert api_key.service == "github"


@pytest.mark.django_db
class TestAPIKeyAdmin:
    """Test the APIKeyAdmin class."""

    def test_list_display(self, api_key_admin):
        """Test that list_display is configured correctly."""
        assert "name" in api_key_admin.list_display
        assert "service_display" in api_key_admin.list_display
        assert "is_active" in api_key_admin.list_display
        assert "created" in api_key_admin.list_display
        assert "updated" in api_key_admin.list_display

    def test_list_filter(self, api_key_admin):
        """Test that list_filter is configured correctly."""
        assert "service" in api_key_admin.list_filter
        assert "is_active" in api_key_admin.list_filter
        assert "created" in api_key_admin.list_filter

    def test_search_fields(self, api_key_admin):
        """Test that search_fields is configured correctly."""
        assert "name" in api_key_admin.search_fields
        assert "description" in api_key_admin.search_fields

    def test_readonly_fields(self, api_key_admin):
        """Test that readonly_fields is configured correctly."""
        assert "created" in api_key_admin.readonly_fields
        assert "updated" in api_key_admin.readonly_fields
        assert "masked_key" in api_key_admin.readonly_fields

    def test_service_display_method(self, api_key_admin):
        """Test the service_display method."""
        api_key = APIKey.objects.create(
            name="Test",
            service="openai",
            api_key="test123",
        )

        display = api_key_admin.service_display(api_key)
        assert display == "OpenAI"

    def test_service_display_unknown_service(self, api_key_admin):
        """Test service_display with unknown service."""
        api_key = APIKey.objects.create(
            name="Test",
            service="unknown_service",
            api_key="test123",
        )

        display = api_key_admin.service_display(api_key)
        assert display == "unknown_service"

    def test_masked_key_method(self, api_key_admin):
        """Test the masked_key method."""
        api_key = APIKey.objects.create(
            name="Test",
            service="openai",
            api_key="sk-1234567890abcdef",
        )

        masked = api_key_admin.masked_key(api_key)
        # Should show first 4 and last 4 characters
        assert masked.startswith("sk-1")
        assert masked.endswith("cdef")
        assert "*" in masked

    def test_masked_key_short_key(self, api_key_admin):
        """Test masked_key with a short key."""
        api_key = APIKey.objects.create(
            name="Test",
            service="openai",
            api_key="short",
        )

        masked = api_key_admin.masked_key(api_key)
        # Short keys should be fully masked
        assert masked == "*****"
        assert "short" not in masked

    def test_masked_key_empty(self, api_key_admin):
        """Test masked_key with empty key."""
        api_key = APIKey.objects.create(
            name="Test",
            service="openai",
            api_key="",
        )

        masked = api_key_admin.masked_key(api_key)
        assert masked == "-"

    def test_masked_key_medium_length(self, api_key_admin):
        """Test masked_key with medium length key (exactly 8 chars)."""
        api_key = APIKey.objects.create(
            name="Test",
            service="openai",
            api_key="12345678",
        )

        masked = api_key_admin.masked_key(api_key)
        # 8 characters should be fully masked
        assert masked == "********"

    def test_masked_key_preserves_security(self, api_key_admin):
        """Test that masked_key never reveals the full key."""
        test_keys = [
            "sk-proj-1234567890abcdefghijklmnopqrstuvwxyz",
            "ghp_verylongkeywithnumbers123456789",
            "xoxb-slack-token-example",
        ]

        for key in test_keys:
            api_key = APIKey(
                name="Test",
                service="test",
                api_key=key,
            )
            masked = api_key_admin.masked_key(api_key)

            # The masked version should be different from original
            assert masked != key
            # Should contain asterisks
            assert "*" in masked
            # Should be approximately the same length or shorter
            assert len(masked) <= len(key)

    def test_fieldsets_structure(self, api_key_admin):
        """Test that fieldsets are properly configured."""
        fieldsets = api_key_admin.fieldsets

        assert len(fieldsets) == 3

        # First fieldset (None) - basic fields
        assert fieldsets[0][0] is None
        assert "name" in fieldsets[0][1]["fields"]
        assert "service" in fieldsets[0][1]["fields"]
        assert "is_active" in fieldsets[0][1]["fields"]

        # Second fieldset - API Key (collapsed)
        assert "API Key" in fieldsets[1][0]
        assert "api_key" in fieldsets[1][1]["fields"]
        assert "masked_key" in fieldsets[1][1]["fields"]
        assert "collapse" in fieldsets[1][1]["classes"]

        # Third fieldset - Details
        assert "Details" in fieldsets[2][0]
        assert "description" in fieldsets[2][1]["fields"]
        assert "created" in fieldsets[2][1]["fields"]
        assert "updated" in fieldsets[2][1]["fields"]

    def test_form_class(self, api_key_admin):
        """Test that the correct form class is used."""
        assert api_key_admin.form == APIKeyAdminForm

    def test_get_queryset(self, api_key_admin, superuser, admin_site):
        """Test that get_queryset works correctly."""
        # Create some API keys
        APIKey.objects.create(name="Key 1", service="openai", api_key="key1")
        APIKey.objects.create(name="Key 2", service="github", api_key="key2")

        # Create a mock request
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/admin/djangocms_automation/apikey/")
        request.user = superuser

        queryset = api_key_admin.get_queryset(request)
        assert queryset.count() == 2

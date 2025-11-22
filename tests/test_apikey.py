"""Tests for the APIKey model."""

import pytest
from django.utils import timezone

from djangocms_automation.models import APIKey


@pytest.mark.django_db
class TestAPIKeyModel:
    """Test the APIKey model."""

    def test_create_api_key(self):
        """Test creating an API key."""
        api_key = APIKey.objects.create(
            name="Test API Key",
            service="openai",
            api_key="sk-test123456789",
            description="Test key for OpenAI",
            is_active=True,
        )

        assert api_key.id is not None
        assert api_key.name == "Test API Key"
        assert api_key.service == "openai"
        assert api_key.api_key == "sk-test123456789"
        assert api_key.description == "Test key for OpenAI"
        assert api_key.is_active is True

    def test_api_key_timestamps(self):
        """Test that created and updated timestamps are set."""
        api_key = APIKey.objects.create(
            name="Test Key",
            service="github",
            api_key="ghp_test123",
        )

        assert api_key.created is not None
        assert api_key.updated is not None
        assert isinstance(api_key.created, type(timezone.now()))
        assert isinstance(api_key.updated, type(timezone.now()))

    def test_api_key_default_values(self):
        """Test default values for optional fields."""
        api_key = APIKey.objects.create(
            name="Minimal Key",
            service="slack",
            api_key="xoxb-test",
        )

        assert api_key.description == ""
        assert api_key.is_active is True

    def test_api_key_str_representation(self):
        """Test the string representation of an API key."""
        api_key = APIKey.objects.create(
            name="My OpenAI Key",
            service="openai",
            api_key="sk-test",
        )

        assert str(api_key) == "My OpenAI Key (OpenAI)"

    def test_api_key_str_with_unknown_service(self):
        """Test string representation with unknown service."""
        api_key = APIKey.objects.create(
            name="Unknown Service Key",
            service="unknown_service",
            api_key="test123",
        )

        # Should fall back to the service ID
        assert str(api_key) == "Unknown Service Key (unknown_service)"

    def test_get_service_display(self):
        """Test getting the service display name."""
        api_key = APIKey.objects.create(
            name="GitHub Key",
            service="github",
            api_key="ghp_test",
        )

        assert api_key.get_service_display() == "GitHub"

    def test_get_service_display_unknown_service(self):
        """Test get_service_display with unknown service."""
        api_key = APIKey.objects.create(
            name="Custom Key",
            service="my_custom_service",
            api_key="custom_key",
        )

        # Should return the service ID if not in registry
        assert api_key.get_service_display() == "my_custom_service"

    def test_get_service_choices(self):
        """Test getting service choices class method."""
        choices = APIKey.get_service_choices()

        assert isinstance(choices, list)
        assert len(choices) > 0

        # Check that choices are tuples of (id, name)
        for choice in choices:
            assert isinstance(choice, tuple)
            assert len(choice) == 2
            assert isinstance(choice[0], str)
            assert isinstance(choice[1], str)

        # Verify some expected services
        choice_ids = [c[0] for c in choices]
        assert "openai" in choice_ids
        assert "github" in choice_ids
        assert "slack" in choice_ids

    def test_multiple_keys_per_service(self):
        """Test that multiple API keys can exist for the same service."""
        APIKey.objects.create(
            name="Production OpenAI",
            service="openai",
            api_key="sk-prod123",
        )
        APIKey.objects.create(
            name="Development OpenAI",
            service="openai",
            api_key="sk-dev456",
        )
        APIKey.objects.create(
            name="Testing OpenAI",
            service="openai",
            api_key="sk-test789",
        )

        openai_keys = APIKey.objects.filter(service="openai")
        assert openai_keys.count() == 3

    def test_api_key_ordering(self):
        """Test that API keys are ordered by service and name."""
        APIKey.objects.create(name="Z Key", service="slack", api_key="key3")
        APIKey.objects.create(name="A Key", service="github", api_key="key1")
        APIKey.objects.create(name="M Key", service="openai", api_key="key2")
        APIKey.objects.create(name="B Key", service="github", api_key="key4")

        all_keys = list(APIKey.objects.all())

        # Should be ordered by service first, then name
        assert all_keys[0].service == "github"
        assert all_keys[0].name == "A Key"
        assert all_keys[1].service == "github"
        assert all_keys[1].name == "B Key"
        assert all_keys[2].service == "openai"
        assert all_keys[3].service == "slack"

    def test_filter_by_active_status(self):
        """Test filtering API keys by active status."""
        APIKey.objects.create(
            name="Active Key 1",
            service="openai",
            api_key="key1",
            is_active=True,
        )
        APIKey.objects.create(
            name="Active Key 2",
            service="github",
            api_key="key2",
            is_active=True,
        )
        APIKey.objects.create(
            name="Inactive Key",
            service="slack",
            api_key="key3",
            is_active=False,
        )

        active_keys = APIKey.objects.filter(is_active=True)
        inactive_keys = APIKey.objects.filter(is_active=False)

        assert active_keys.count() == 2
        assert inactive_keys.count() == 1

    def test_filter_by_service(self):
        """Test filtering API keys by service."""
        APIKey.objects.create(name="OpenAI 1", service="openai", api_key="key1")
        APIKey.objects.create(name="OpenAI 2", service="openai", api_key="key2")
        APIKey.objects.create(name="GitHub 1", service="github", api_key="key3")

        openai_keys = APIKey.objects.filter(service="openai")
        github_keys = APIKey.objects.filter(service="github")

        assert openai_keys.count() == 2
        assert github_keys.count() == 1

    def test_update_api_key(self):
        """Test updating an API key."""
        api_key = APIKey.objects.create(
            name="Original Name",
            service="openai",
            api_key="original_key",
            is_active=True,
        )

        original_updated = api_key.updated

        # Update the key
        api_key.name = "Updated Name"
        api_key.api_key = "new_key"
        api_key.is_active = False
        api_key.save()

        # Refresh from database
        api_key.refresh_from_db()

        assert api_key.name == "Updated Name"
        assert api_key.api_key == "new_key"
        assert api_key.is_active is False
        # Updated timestamp should have changed
        assert api_key.updated > original_updated

    def test_delete_api_key(self):
        """Test deleting an API key."""
        api_key = APIKey.objects.create(
            name="To Delete",
            service="openai",
            api_key="delete_me",
        )

        key_id = api_key.id
        assert APIKey.objects.filter(id=key_id).exists()

        api_key.delete()

        assert not APIKey.objects.filter(id=key_id).exists()

    def test_api_key_max_lengths(self):
        """Test that max lengths are respected."""
        # Create with maximum lengths
        api_key = APIKey.objects.create(
            name="A" * 255,  # Max length for name
            service="B" * 100,  # Max length for service
            api_key="C" * 500,  # Max length for api_key
        )

        assert len(api_key.name) == 255
        assert len(api_key.service) == 100
        assert len(api_key.api_key) == 500

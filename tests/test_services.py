"""Tests for the service registry system."""


from djangocms_automation.services import ServiceRegistry, service_registry


class TestServiceRegistry:
    """Test the ServiceRegistry class."""

    def test_registry_initialization(self):
        """Test that a new registry initializes empty."""
        registry = ServiceRegistry()
        assert registry._services == {}

    def test_register_service(self):
        """Test registering a new service."""
        registry = ServiceRegistry()
        registry.register("test_service", "Test Service", "A test service")

        service = registry.get("test_service")
        assert service is not None
        assert service["id"] == "test_service"
        assert service["name"] == "Test Service"
        assert service["description"] == "A test service"

    def test_register_service_without_description(self):
        """Test registering a service without description."""
        registry = ServiceRegistry()
        registry.register("test_service", "Test Service")

        service = registry.get("test_service")
        assert service is not None
        assert service["description"] == ""

    def test_unregister_service(self):
        """Test unregistering a service."""
        registry = ServiceRegistry()
        registry.register("test_service", "Test Service")
        assert registry.get("test_service") is not None

        registry.unregister("test_service")
        assert registry.get("test_service") is None

    def test_unregister_nonexistent_service(self):
        """Test unregistering a service that doesn't exist."""
        registry = ServiceRegistry()
        # Should not raise an error
        registry.unregister("nonexistent")

    def test_get_service(self):
        """Test getting a service by ID."""
        registry = ServiceRegistry()
        registry.register("test_service", "Test Service")

        service = registry.get("test_service")
        assert service["id"] == "test_service"

    def test_get_nonexistent_service(self):
        """Test getting a service that doesn't exist."""
        registry = ServiceRegistry()
        service = registry.get("nonexistent")
        assert service is None

    def test_all_services(self):
        """Test getting all services."""
        registry = ServiceRegistry()
        registry.register("service1", "Service 1")
        registry.register("service2", "Service 2")
        registry.register("service3", "Service 3")

        all_services = registry.all()
        assert len(all_services) == 3
        service_ids = [s["id"] for s in all_services]
        assert "service1" in service_ids
        assert "service2" in service_ids
        assert "service3" in service_ids

    def test_get_choices(self):
        """Test getting service choices for Django field."""
        registry = ServiceRegistry()
        registry.register("service1", "Service 1")
        registry.register("service2", "Service 2")

        choices = registry.get_choices()
        assert len(choices) == 2
        assert ("service1", "Service 1") in choices
        assert ("service2", "Service 2") in choices

    def test_get_choices_preserves_order(self):
        """Test that get_choices returns choices in a consistent order."""
        registry = ServiceRegistry()
        registry.register("z_service", "Z Service")
        registry.register("a_service", "A Service")
        registry.register("m_service", "M Service")

        choices = registry.get_choices()
        assert len(choices) == 3
        # Choices should contain all registered services
        choice_ids = [c[0] for c in choices]
        assert "z_service" in choice_ids
        assert "a_service" in choice_ids
        assert "m_service" in choice_ids


class TestGlobalServiceRegistry:
    """Test the global service_registry instance."""

    def test_global_registry_exists(self):
        """Test that the global registry exists."""
        assert service_registry is not None
        assert isinstance(service_registry, ServiceRegistry)

    def test_global_registry_has_default_services(self):
        """Test that the global registry has pre-registered services."""
        # Check some common services
        openai = service_registry.get("openai")
        assert openai is not None
        assert openai["name"] == "OpenAI"

        github = service_registry.get("github")
        assert github is not None
        assert github["name"] == "GitHub"

        slack = service_registry.get("slack")
        assert slack is not None
        assert slack["name"] == "Slack"

    def test_global_registry_service_count(self):
        """Test that the global registry has the expected number of services."""
        all_services = service_registry.all()
        # Should have at least the pre-registered services
        assert len(all_services) >= 10

    def test_global_registry_custom_service(self):
        """Test that the global registry includes a custom service option."""
        custom = service_registry.get("custom")
        assert custom is not None
        assert custom["name"] == "Custom Service"

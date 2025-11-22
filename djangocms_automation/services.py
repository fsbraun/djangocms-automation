"""Service registry for automation integrations."""


class ServiceRegistry:
    """Registry for external services that can be used in automations."""

    def __init__(self):
        self._services = {}

    def register(self, service_id, name, description=""):
        """
        Register a service.

        Args:
            service_id: Unique identifier for the service
            name: Human-readable name
            description: Optional description of the service
        """
        self._services[service_id] = {
            "id": service_id,
            "name": name,
            "description": description,
        }

    def unregister(self, service_id):
        """Unregister a service."""
        self._services.pop(service_id, None)

    def get(self, service_id):
        """Get service information by ID."""
        return self._services.get(service_id)

    def all(self):
        """Get all registered services."""
        return list(self._services.values())

    def get_choices(self):
        """Get service choices for Django field choices."""
        return [(service["id"], service["name"]) for service in self._services.values()]


# Global service registry instance
service_registry = ServiceRegistry()


# Register some common services
service_registry.register("openai", "OpenAI", "OpenAI API for GPT models")
service_registry.register("anthropic", "Anthropic", "Anthropic API for Claude models")
service_registry.register("google", "Google", "Google Cloud APIs")
service_registry.register("aws", "Amazon Web Services", "AWS APIs")
service_registry.register("slack", "Slack", "Slack API")
service_registry.register("github", "GitHub", "GitHub API")
service_registry.register("stripe", "Stripe", "Stripe payment API")
service_registry.register("sendgrid", "SendGrid", "SendGrid email API")
service_registry.register("twilio", "Twilio", "Twilio SMS API")
service_registry.register("custom", "Custom Service", "Custom API service")

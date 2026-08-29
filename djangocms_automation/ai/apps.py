from django.apps import AppConfig


class AutomationAIConfig(AppConfig):
    """The AI surface as its own Django app.

    It is an app rather than a plain subpackage so that django CMS discovers its
    ``cms_plugins`` module on its own. Without that, the core package would have
    to import this one to register the LLM plugin, which is exactly the
    dependency this arrangement exists to avoid.

    Models declared here keep the ``djangocms_automation`` app label, so nothing
    about this move needs a migration.
    """

    name = "djangocms_automation.ai"
    label = "djangocms_automation_ai"
    verbose_name = "django CMS Automation: AI"

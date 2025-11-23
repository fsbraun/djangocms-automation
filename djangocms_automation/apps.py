from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DjangocmsAutomationConfig(AppConfig):
    name = "djangocms_automation"
    verbose_name = _("Automation")

    def ready(self):
        from . import models
        from .cms_plugins import automation_plugins

        models.AutomationContent.allowed_plugins = automation_plugins

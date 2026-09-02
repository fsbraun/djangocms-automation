from cms.app_base import CMSAppConfig

from .models import AutomationContent
from .views import AutomationView


class AutomationCMSConfig(CMSAppConfig):
    """What django CMS needs to know about this app.

    Versioning is reached through the CMS contract rather than by importing it.
    ``get_contract`` asks the registered extension apps for the class exported
    under a name — versioning publishes ``("djangocms_versioning",
    VersionableItem)`` — so this module needs no import of a package that is
    not a dependency, and simply builds no versionables when it is absent.
    """

    cms_enabled = True
    cms_toolbar_enabled_models = [(AutomationContent, AutomationView.as_view(), "automation")]
    djangocms_versioning_enabled = True

    def __init__(self, app):
        super().__init__(app)
        versionable_item = self.get_contract("djangocms_versioning")
        if versionable_item is None:
            # Versioning is not installed. Nothing to register, and nothing to
            # raise about: an automation without versions is a working
            # automation.
            self.djangocms_versioning_enabled = False
            self.versioning = []
            return
        self.versioning = [
            versionable_item(
                content_model=AutomationContent,
                grouper_field_name="automation",
                # Ours, so nothing outside this module knows versioning exists.
                copy_function=AutomationContent.copy,
                grouper_admin_mixin="__default__",
            ),
        ]

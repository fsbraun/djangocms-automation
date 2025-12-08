from cms.app_base import CMSAppConfig

from .models import AutomationContent
from .views import AutomationView


class StoriesCMSConfig(CMSAppConfig):
    cms_enabled = True
    cms_toolbar_enabled_models = [(AutomationContent, AutomationView.as_view(), "automation")]
    djangocms_versioning_enabled = True
    if djangocms_versioning_enabled:
        from packaging.version import Version as PackageVersion
        from djangocms_versioning import __version__ as djangocms_versioning_version
        from djangocms_versioning.datastructures import default_copy, VersionableItem

        if PackageVersion(djangocms_versioning_version) < PackageVersion("2.4"):  # pragma: no cover
            raise ImportError(
                "djangocms_versioning >= 2.4.0 is required for djangocms_stories to work properly."
                " Please upgrade djangocms_versioning."
            )

        versioning = [
            VersionableItem(
                content_model=AutomationContent,
                grouper_field_name="automation",
                copy_function=default_copy,
                grouper_admin_mixin="__default__",
            ),
        ]

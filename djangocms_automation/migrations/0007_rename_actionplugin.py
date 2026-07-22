from django.db import migrations


def forwards(apps, schema_editor):
    """Rename the plugin_type of the base action CMS plugin.

    The CMS plugin class ``AutomationAction`` was renamed to ``ActionPlugin``
    to resolve the name collision with the runtime model
    ``djangocms_automation.instances.AutomationAction``.
    """
    CMSPlugin = apps.get_model("cms", "CMSPlugin")
    CMSPlugin.objects.filter(plugin_type="AutomationAction").update(plugin_type="ActionPlugin")


def backwards(apps, schema_editor):
    CMSPlugin = apps.get_model("cms", "CMSPlugin")
    CMSPlugin.objects.filter(plugin_type="ActionPlugin").update(plugin_type="AutomationAction")


class Migration(migrations.Migration):
    dependencies = [
        ("cms", "0001_initial"),
        ("djangocms_automation", "0006_engine_hardening"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

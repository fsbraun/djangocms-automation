"""Give every executable node a reader-facing name."""

import re

from django.db import migrations, models


TYPE_NAMES = {
    "AIStep": "Ask a Model",
    "CreateModelAction": "Create Record",
    "MailAction": "Send Email",
    "QueryModelAction": "Query Records",
    "UpdateModelAction": "Update Records",
    "UserInputAction": "Wait for User",
}

def _type_name(plugin_type):
    if plugin_type in TYPE_NAMES:
        return TYPE_NAMES[plugin_type]
    name = re.sub(r"(?:Plugin|Action|Model)$", "", plugin_type or "")
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name) or "Automation step"


def populate_intents(apps, schema_editor):
    Conditional = apps.get_model("djangocms_automation", "ConditionalPluginModel")
    Loop = apps.get_model("djangocms_automation", "LoopPluginModel")
    Split = apps.get_model("djangocms_automation", "SplitPluginModel")
    Action = apps.get_model("djangocms_automation", "BaseActionPluginModel")
    CMSPlugin = apps.get_model("cms", "CMSPlugin")

    for node in Conditional.objects.all():
        node.intent = (node.question or "").strip() or "Conditional"
        node.save(update_fields=["intent"])
    for node in Loop.objects.all():
        node.intent = (node.question or "").strip() or "Loop"
        node.save(update_fields=["intent"])
    Split.objects.update(intent="Split")

    plugin_types = dict(CMSPlugin.objects.filter(pk__in=Action.objects.values("pk")).values_list("pk", "plugin_type"))
    for node in Action.objects.all():
        plugin_type = plugin_types.get(node.pk, "")
        node.intent = _type_name(plugin_type)
        node.save(update_fields=["intent"])


class Migration(migrations.Migration):

    dependencies = [
        ("djangocms_automation", "0021_field_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="baseactionpluginmodel",
            name="intent",
            field=models.CharField(
                default="",
                help_text="Name what this step achieves with a verb and noun, e.g. 'Notify the customer'.",
                max_length=255,
                verbose_name="Intent",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="conditionalpluginmodel",
            name="intent",
            field=models.CharField(
                default="",
                help_text="Name what this step achieves with a verb and noun, e.g. 'Notify the customer'.",
                max_length=255,
                verbose_name="Intent",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="looppluginmodel",
            name="intent",
            field=models.CharField(
                default="",
                help_text="Name what this step achieves with a verb and noun, e.g. 'Notify the customer'.",
                max_length=255,
                verbose_name="Intent",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="splitpluginmodel",
            name="intent",
            field=models.CharField(
                default="",
                help_text="Name what this step achieves with a verb and noun, e.g. 'Notify the customer'.",
                max_length=255,
                verbose_name="Intent",
            ),
            preserve_default=False,
        ),
        migrations.RunPython(populate_intents, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="conditionalpluginmodel",
            name="question",
        ),
        migrations.RemoveField(
            model_name="looppluginmodel",
            name="question",
        ),
    ]

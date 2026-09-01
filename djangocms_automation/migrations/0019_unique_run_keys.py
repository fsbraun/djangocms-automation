"""Give every existing run a key that names only itself.

The key was computed before the insert, so ``self.id`` was ``None`` and a run
never saved a second time kept ``sha1("<automation>-None")`` — the same string
for every such run of that automation. Those keys identify nothing, and the
admin now shows them as a run's name, so they are recomputed here.

Only those. A key that already includes the row id is a reference someone may
have copied, and it is left exactly as it is.
"""

import hashlib

from django.db import migrations


def _key(automation_id, instance_id):
    return hashlib.sha1(f"{automation_id}-{instance_id}".encode()).hexdigest()


def name_each_run(apps, schema_editor):
    AutomationInstance = apps.get_model("djangocms_automation", "AutomationInstance")
    rows = AutomationInstance.objects.select_related("automation_content").only(
        "id", "key", "automation_content__automation_id"
    )
    fixed = []
    for instance in rows.iterator():
        automation_id = instance.automation_content.automation_id
        if instance.key and instance.key != _key(automation_id, None):
            continue  # already names this run
        instance.key = _key(automation_id, instance.id)
        fixed.append(instance)
    if fixed:
        AutomationInstance.objects.bulk_update(fixed, ["key"], batch_size=500)


def leave_them(apps, schema_editor):
    """Nothing to undo: the old keys named nothing, so there is nothing to
    restore them to."""


class Migration(migrations.Migration):
    dependencies = [("djangocms_automation", "0018_tools_are_actions")]

    operations = [migrations.RunPython(name_each_run, leave_them)]

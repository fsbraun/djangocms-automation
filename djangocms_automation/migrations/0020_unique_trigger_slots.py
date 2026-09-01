"""One slot per trigger, within an automation.

A trigger's slot names the placeholder holding its flow. Two triggers sharing
one used to work by accident — both resolved to the same placeholder and ran
the same flow — but renaming either now renames that placeholder, which would
leave two placeholders with one name and ``.get(slot=…)`` raising for both.

Existing duplicates are given their own slots before the constraint goes on,
because a migration that cannot apply is worse than a workflow that needs
looking at.
"""

from django.db import migrations, models


def separate_shared_slots(apps, schema_editor):
    AutomationTrigger = apps.get_model("djangocms_automation", "AutomationTrigger")
    triggers = list(AutomationTrigger.objects.order_by("automation_content_id", "position", "pk"))
    # Every name already in use, gathered before anything moves. A trigger
    # legitimately called ``start-2`` must not be pushed aside by a duplicate
    # of ``start`` arriving at the same name.
    taken = {(trigger.automation_content_id, trigger.slot) for trigger in triggers}

    seen = set()
    renamed = []
    for trigger in triggers:
        key = (trigger.automation_content_id, trigger.slot)
        if key not in seen:
            seen.add(key)
            continue
        # The first keeps the name; the rest are numbered off it. Their flow
        # stays where it is — the placeholder keeps the original slot — so this
        # shows up as a trigger that needs re-pointing rather than one silently
        # rewired to somebody else's flow.
        suffix = 2
        while (trigger.automation_content_id, f"{trigger.slot}-{suffix}") in taken | seen:
            suffix += 1
        trigger.slot = f"{trigger.slot}-{suffix}"[:255]
        seen.add((trigger.automation_content_id, trigger.slot))
        renamed.append(trigger)
    if renamed:
        AutomationTrigger.objects.bulk_update(renamed, ["slot"], batch_size=500)


def leave_them(apps, schema_editor):
    """Nothing to undo: the renames are the state the constraint requires."""


class Migration(migrations.Migration):
    dependencies = [("djangocms_automation", "0019_unique_run_keys")]

    operations = [
        migrations.RunPython(separate_shared_slots, leave_them),
        migrations.AddConstraint(
            model_name="automationtrigger",
            constraint=models.UniqueConstraint(
                fields=("automation_content", "slot"),
                name="unique_trigger_slot_per_automation",
                violation_error_message=(
                    "Another trigger on this automation already uses this slot. "
                    "The slot is how a trigger finds its own flow, so no two can share one."
                ),
            ),
        ),
    ]

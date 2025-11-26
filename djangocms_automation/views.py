from django.contrib.contenttypes.models import ContentType
from django.views.generic import DetailView

from cms.models import Placeholder

from .models import AutomationContent


class AutomationView(DetailView):
    model = AutomationContent
    template_name = "djangocms_automation/automation_detail.html"
    context_object_name = "automation_content"

    def get_object(self, queryset=None):
        content_object = self.args[0]

        # Additional logic can be added here if needed
        return content_object

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        obj = self.get_object()
        triggers = list(obj.triggers.all())
        slots = [trigger.slot for trigger in triggers]

        if slots:
            ct = ContentType.objects.get_for_model(obj)
            # Fetch existing placeholders for these slots
            placeholders = {
                placeholder.slot: placeholder
                for placeholder in Placeholder.objects.filter(
                    content_type=ct,
                    object_id=obj.pk,
                    slot__in=slots,
                )
            }
            existing_slots = set(placeholders.keys())

            # Create missing placeholders in bulk
            missing = [slot for slot in slots if slot not in existing_slots]
            if missing:
                to_create = [Placeholder(slot=slot, content_type=ct, object_id=obj.pk) for slot in missing]
                try:
                    # Prefer ignoring conflicts if DB has a uniqueness constraint
                    Placeholder.objects.bulk_create(to_create, ignore_conflicts=True)
                except TypeError:
                    # Fallback for older Django versions without ignore_conflicts
                    Placeholder.objects.bulk_create(to_create)

                # Re-query to include newly created placeholders
                placeholders = {
                    placeholder.slot: placeholder
                    for placeholder in Placeholder.objects.filter(
                        content_type=ct,
                        object_id=obj.pk,
                        slot__in=slots,
                    )
                }
        else:
            placeholders = {}

        for trigger in triggers:
            trigger.placeholder = placeholders.get(trigger.slot)

        triggers = list(triggers)
        context["triggers"] = triggers
        return context

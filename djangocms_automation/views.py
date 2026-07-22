from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import DetailView

from cms.models import Placeholder
from cms.utils import get_language_from_request
from cms.toolbar.utils import get_object_edit_url, get_object_preview_url

from .models import AutomationContent, AutomationTrigger
from .triggers import WebhookTrigger


@method_decorator(csrf_exempt, name="dispatch")
class WebhookView(View):
    """Inbound webhook receiver for webhook-based triggers.

    ``POST .../webhook/<token>/`` fires every active trigger whose config
    carries the token and whose type is a :class:`WebhookTrigger` subclass.
    The trigger definition authenticates the request (optional HMAC
    signature), parses/filters the payload into data rows, and the rows are
    validated against the trigger's data schema before execution.

    Responses: 200 ``{"triggered": N, "filtered": M}``; 404 unknown token;
    403 failed signature; 400 malformed or schema-invalid payload.
    """

    http_method_names = ["post"]

    def post(self, request, token):
        triggers = AutomationTrigger.objects.filter(
            config__token=token,
            automation_content__automation__is_active=True,
        ).select_related("automation_content")

        idempotency_key = request.headers.get("X-Idempotency-Key")
        matched = False
        fired = 0
        filtered = 0
        for trigger in triggers:
            definition = trigger.get_definition()
            if definition is None or not issubclass(definition, WebhookTrigger):
                continue
            matched = True
            handler = definition()

            if not handler.verify_request(request, trigger.config):
                return JsonResponse({"error": "Invalid signature."}, status=403)
            try:
                rows = handler.parse_payload(request, trigger.config)
            except ValueError:
                return JsonResponse({"error": "Could not parse the request payload."}, status=400)
            if not rows:
                filtered += 1
                continue
            for row in rows:
                if not handler.validate_payload(row, raise_errors=False):
                    return JsonResponse({"error": "Payload does not match the trigger's data schema."}, status=400)
            try:
                trigger.trigger_execution(data=rows, start=True, idempotency_key=idempotency_key)
            except Exception:
                return JsonResponse({"error": "Automation execution failed."}, status=500)
            fired += 1

        if not matched:
            raise Http404("No active webhook trigger for this token.")
        return JsonResponse({"triggered": fired, "filtered": filtered})


class AutomationView(DetailView):
    model = AutomationContent
    template_name = "djangocms_automation/automation_detail.html"
    context_object_name = "automation_content"

    def dispatch(self, request, *args, **kwargs):
        if get_language_from_request(request) != settings.LANGUAGE_CODE:
            # Automations are only available in the default language (no language-specific content)
            if request.toolbar and request.toolbar.edit_mode_active:
                return HttpResponseRedirect(
                    get_object_edit_url(request.toolbar.get_object(), language=settings.LANGUAGE_CODE)
                )
            return HttpResponseRedirect(
                get_object_preview_url(request.toolbar.get_object(), language=settings.LANGUAGE_CODE)
            )
        return super().dispatch(request, *args, **kwargs)

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

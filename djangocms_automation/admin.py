import json

from django import forms
from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from cms.admin.utils import GrouperModelAdmin

from .forms import AutomationTriggerAdminForm
from .models import Automation, AutomationContent, APIKey, AutomationTrigger
from .instances import AutomationInstance, AutomationAction
from .triggers import trigger_registry


@admin.register(Automation)
class AutomationAdmin(GrouperModelAdmin):
    content_model = AutomationContent
    grouper_field_name = "automation"

    def save_related(self, request, form, formsets, change):
        """After saving automation content, ensure it has a default trigger."""
        super().save_related(request, form, formsets, change)
        triggerless_automation_contents = AutomationContent.admin_manager.current_content().filter(
            automation=form.instance,
            triggers__isnull=True,
        )
        for automation_content in triggerless_automation_contents:
            AutomationTrigger.objects.create(
                automation_content=automation_content,
                slot="start",
            )


@admin.register(AutomationContent)
class AutomationContentAdmin(admin.ModelAdmin):
    def get_model_perms(self, request):
        """
        Return empty perms dict thus hiding the model from admin index.
        """
        return {}


class AutomationActionInline(admin.TabularInline):
    model = AutomationAction
    extra = 0
    fields = (
        "status",
        "message",
        "requires_interaction",
        "interaction_user",
        "interaction_group",
        "created",
        "finished",
        "locked",
    )
    readonly_fields = ("created", "finished")
    can_delete = False


@admin.register(AutomationInstance)
class AutomationInstanceAdmin(admin.ModelAdmin):
    list_display = ("id", "automation", "created", "updated")
    list_filter = ("automation", "created")
    search_fields = ("key", "automation__name")
    readonly_fields = ("key", "created", "updated", "data_display")
    inlines = [AutomationActionInline]
    fieldsets = (
        (None, {"fields": ("automation", "finished", "key")}),
        ("Timing", {"fields": ("paused_until", "created", "updated")}),
        ("Data", {"fields": ("data_display",), "classes": ("collapse",)}),
    )

    def data_display(self, obj):
        """Display JSON data in a formatted, readable way."""
        if obj.data:
            formatted_json = json.dumps(obj.data, indent=2, ensure_ascii=False)
            return format_html('<pre style="margin: 0;">{}</pre>', formatted_json)
        return "-"

    data_display.short_description = "Data (formatted)"


class APIKeyAdminForm(forms.ModelForm):
    """Custom form for APIKey to use dynamic service choices."""

    class Meta:
        model = APIKey
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Dynamically set service choices from registry
        self.fields["service"].widget = forms.Select(choices=APIKey.get_service_choices())


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    form = APIKeyAdminForm
    list_display = ("name", "service_display", "is_active", "created", "updated")
    list_filter = ("service", "is_active", "created")
    search_fields = ("name", "description")
    readonly_fields = ("created", "updated", "masked_key")
    fieldsets = (
        (None, {"fields": ("name", "service", "is_active")}),
        ("API Key", {"fields": ("api_key", "masked_key"), "classes": ("collapse",)}),
        ("Details", {"fields": ("description", "created", "updated")}),
    )

    def service_display(self, obj):
        """Display the human-readable service name."""
        return obj.get_service_display()

    service_display.short_description = "Service"
    service_display.admin_order_field = "service"

    def masked_key(self, obj):
        """Display a masked version of the API key for security."""
        if obj.api_key:
            key_length = len(obj.api_key)
            if key_length > 8:
                return f"{obj.api_key[:4]}{'*' * (key_length - 8)}{obj.api_key[-4:]}"
            return "*" * key_length
        return "-"

    masked_key.short_description = "Masked Key"


@admin.register(AutomationTrigger)
class AutomationTriggerAdmin(admin.ModelAdmin):
    """Admin for AutomationTrigger: add/change views available, hidden from index."""

    name = _("Trigger")
    form = AutomationTriggerAdminForm
    readonly_fields = ("position",)
    ordering = ("automation_content", "position")

    change_form_template = "djangocms_frontend/admin/base.html"

    class Media:
        js = ('djangocms_automation/js/trigger_type_change.js',)
        css = {
            'all': ('djangocms_automation/css/trigger_admin.css',)
        }

    @staticmethod
    def get_trigger(request, obj) -> tuple[forms.Form | None, bool]:
        trigger_type = request.POST.get('_trigger_type_change') if request.method == 'POST' else None
        fallback = trigger_type or (obj.type if obj else request.GET.get('type') or "click")
        return trigger_registry.get(trigger_type or fallback), trigger_type is not None

    def get_fieldsets(self, request, obj=None):
        """Return fieldsets with dynamic config fields based on trigger type."""
        base_fieldsets = [
            (None, {
                'fields': ('automation_content', 'type', 'slot', 'position')
            }),
        ]
        trigger_class, changed = self.get_trigger(request, obj)
        # Add config fieldset if trigger has config fields
        if trigger_class and not changed:
            if trigger_class.declared_fields:
                # Get all config field names (with config_ prefix)
                base_fieldsets.append((
                   trigger_class.name,
                    {
                        'fields': list(trigger_class.declared_fields.keys()),
                        'classes': ('collapse',),
                    }
                ))
        return base_fieldsets

    def get_form(self, request, obj=None, **kwargs):
        """Customize form instance with request context."""
        trigger_class, changed = self.get_trigger(request, obj)
        if trigger_class and not changed:
            self.form = type("FormWithTriggerConfig", (AutomationTriggerAdminForm, trigger_class), {})
        else:
            self.form = AutomationTriggerAdminForm

        form = super().get_form(request, obj, **kwargs)

        # Add localized confirmation message as data attribute
        if 'type' in form.base_fields:
            form.base_fields['type'].widget.attrs['data-confirm-message'] = str(_(
                "Changing the trigger type will reload the form with different configuration fields. "
                "Current configuration will not be saved. Continue?"
            ))

        return form

    def save_model(self, request, obj, form, change):
        """Handle type changes during save."""
        # Check if this is a type change
        if '_trigger_type_change' in request.POST:
            new_type = request.POST.get('_trigger_type_change')
            if new_type:
                obj.type = new_type
                # Clear old config when changing type
                obj.config = {}

        super().save_model(request, obj, form, change)

    def response_change(self, request, obj):
        """Redirect to change form with new type after type change."""
        if '_trigger_type_change' in request.POST:
            # Redirect to same change form to reload with new fields
            from django.http import HttpResponseRedirect
            from django.urls import reverse
            url = reverse('admin:%s_%s_change' % (
                obj._meta.app_label,
                obj._meta.model_name),
                args=[obj.pk]
            )
            return HttpResponseRedirect(url)

        return super().response_change(request, obj)

    def get_model_perms(self, request):  # Hides from admin index/app list
        return {}

    def __str__(self):
        return str(self.name)
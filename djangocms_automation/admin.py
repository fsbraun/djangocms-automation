import json

from django.contrib import admin
from django.utils.html import format_html
from django import forms

from cms.admin.utils import GrouperModelAdmin

from .models import Automation, AutomationContent, APIKey
from .instances import AutomationInstance, AutomationAction




@admin.register(Automation)
class AutomationAdmin(GrouperModelAdmin):
    ordering = ("name",)
    content_model = AutomationContent
    grouper_field_name = "automation"


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
    list_display = ("id", "automation_class", "finished", "created", "updated", "paused_until")
    list_filter = ("finished", "automation_class", "created")
    search_fields = ("key", "automation_class__name")
    readonly_fields = ("key", "created", "updated", "data_display")
    inlines = [AutomationActionInline]
    fieldsets = (
        (None, {"fields": ("automation_class", "finished", "key")}),
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

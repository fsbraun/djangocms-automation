from django.contrib import admin

from cms.admin.utils import GrouperModelAdmin

from .models import Automation, AutomationContent


@admin.register(Automation)
class AutomationAdmin(GrouperModelAdmin):
    ordering = ("name",)
    content_model = AutomationContent
    grouper_field_name = "automation"


@admin.register(AutomationContent)
class AutomationContentAdmin(admin.ModelAdmin):
    pass

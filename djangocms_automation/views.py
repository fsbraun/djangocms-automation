from django.views.generic import DetailView

from .models import AutomationContent


class AutomationView(DetailView):
    model = AutomationContent
    template_name = "djangocms_automation/automation_detail.html"
    context_object_name = "automation_content"

    def get_object(self, queryset=None):
        content_object = self.args[0]

        # Additional logic can be added here if needed
        return content_object

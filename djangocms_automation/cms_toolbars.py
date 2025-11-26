from django.urls import reverse
from cms.toolbar_base import CMSToolbar
from cms.toolbar_pool import toolbar_pool
from django.utils.translation import gettext_lazy as _

from .models import AutomationTrigger, AutomationContent


@toolbar_pool.register
class AutomationToolbar(CMSToolbar):
    """Adds an 'Automation' menu to the CMS toolbar."""

    def populate(self):
        if not isinstance(self.toolbar.get_object(), AutomationContent):
            return

        automation_content = self.toolbar.get_object()
        user = self.request.user
        can_view_triggers = user.has_perm("djangocms_automation.view_automationtrigger")

        if not can_view_triggers:
            return

        # Create the main Automation menu
        menu = self.toolbar.get_or_create_menu("automation-menu", _("Automation"))

        # Add "Triggers" entry that opens the changelist in a modal, filtered by automation_content
        url = reverse("admin:djangocms_automation_automationtrigger_changelist")
        url += f"?automation_content={automation_content.pk}"
        menu.add_modal_item(_("Triggers"), url)


@toolbar_pool.register
class AutomationTriggerToolbar(CMSToolbar):
    """Adds a 'Triggers' menu to the CMS toolbar with quick access links."""

    def x_populate(self):
        if not isinstance(self.toolbar.get_object(), AutomationContent):  # or not self.toolbar.edit_mode_active:
            return
        user = self.request.user
        can_add = user.has_perm("djangocms_automation.add_automationtrigger")
        can_change = user.has_perm("djangocms_automation.change_automationtrigger")

        menu = self.toolbar.get_or_create_menu("automation-trigger-menu", _("Triggers"))

        if can_add:
            url = (
                reverse("admin:djangocms_automation_automationtrigger_add")
                + f"?automation_content={self.toolbar.get_object().pk}"
            )
            menu.add_modal_item(_("Add Trigger"), url)

        # List all triggers with modal edit (or disabled if lacking change perm)
        triggers = (
            AutomationTrigger.objects.filter(automation_content=self.toolbar.get_object())
            .select_related("automation_content", "automation_content__automation")
            .all()
        )
        if triggers:
            menu.add_break(_("All Triggers"))
            for trigger in triggers:
                label = str(trigger)
                if can_change:
                    url = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])
                    menu.add_modal_item(label, url)
                else:
                    # User can see list (due to add?) but cannot change individual triggers
                    menu.add_disabled_item(label)

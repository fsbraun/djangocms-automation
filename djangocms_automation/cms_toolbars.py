from cms.toolbar_base import CMSToolbar
from cms.toolbar_pool import toolbar_pool
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .models import AutomationContent, AutomationTrigger


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

        self.populate_run_item(menu, automation_content)

        # Create a submenu for triggers
        trigger_menu = menu.get_or_create_menu("automation-trigger-submenu", _("Triggers"))
        self.populate_trigger_menu(trigger_menu, automation_content)

    def populate_run_item(self, menu, automation_content):
        """The button for trying the thing you just built.

        Every other way in needs something outside the editor — a webhook
        delivery, a cron tick, a form submission, a shell. Which made the
        moment right after building an automation the one moment there was no
        way to run it.

        Disabled rather than hidden when there is nothing to start: an
        automation with no trigger has no entry point at all, and saying so
        here is more use than an *Automation* menu that quietly lacks an item.
        """
        if not self.request.user.has_perm("djangocms_automation.add_automationinstance"):
            return
        menu.add_break("automation-run-break")
        if not AutomationTrigger.objects.filter(automation_content=automation_content).exists():
            menu.add_disabled_item(_("Run now…"))
            return
        url = reverse("admin:djangocms_automation_run_now") + f"?automation_content={automation_content.pk}"
        menu.add_modal_item(_("Run now…"), url)

    def populate_trigger_menu(self, menu, automation_content):
        if not isinstance(self.toolbar.get_object(), AutomationContent):  # or not self.toolbar.edit_mode_active:
            return
        user = self.request.user
        can_add = user.has_perm("djangocms_automation.add_automationtrigger")
        can_change = user.has_perm("djangocms_automation.change_automationtrigger")

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

        # Add "Triggers" entry that opens the changelist in a modal, filtered by automation_content
        menu.add_break()
        url = reverse("admin:djangocms_automation_automationtrigger_changelist")
        url += f"?automation_content={automation_content.pk}"
        menu.add_modal_item(_("Triggers"), url)

from cms.admin.utils import GrouperModelAdmin
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

        self.populate_properties_item(menu, automation_content)
        self.populate_run_item(menu, automation_content)
        self.populate_runs_item(menu, automation_content)

        # Create a submenu for triggers
        menu.add_break("trigger-break")
        trigger_menu = menu.get_or_create_menu("automation-trigger-submenu", _("Triggers"))
        # A published version's flows cannot be touched, and a trigger is a
        # name for a flow — so the list is still worth reading, and none of it
        # is clickable. The same is true of somebody who may look and not
        # change, which is why one flag answers both.
        self.populate_trigger_menu(
            trigger_menu, automation_content, automation_content.placeholder_editable(self.request)
        )

    def populate_properties_item(self, menu, automation_content):
        """What the automation *is*, above what it does."""
        automation = getattr(automation_content, "automation", None)
        if automation is None:
            return
        if self.request.user.has_perm("djangocms_automation.change_automation"):
            # ``cms_content`` names which version's content the grouper form
            # should show. Without it the change view falls back to the latest,
            # so opening the properties of the version being edited could hand
            # back somebody else's draft.
            url = (
                reverse("admin:djangocms_automation_automation_change", args=[automation.pk])
                + f"?{GrouperModelAdmin.content_pk_url_param}={automation_content.pk}"
            )
            menu.add_modal_item(_("Automation properties…"), url)
        else:
            # Listed and not clickable, like everything else here somebody may
            # not do. That an automation *has* properties is not a secret; what
            # the permission gates is changing them.
            menu.add_link_item(_("Automation properties…"), url="", disabled=True)
        menu.add_break()

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
        if not AutomationTrigger.objects.filter(automation_content=automation_content).exists():
            # ``disabled=True`` on a real item: django CMS has no
            # ``add_disabled_item`` — every ``add_*_item`` takes the flag
            # instead, and the item renders greyed out.
            menu.add_link_item(_("Run now…"), url="", disabled=True)
            return
        url = reverse("admin:djangocms_automation_run_now") + f"?automation_content={automation_content.pk}"
        menu.add_modal_item(_("Run now…"), url)

    def populate_runs_item(self, menu, automation_content):
        """Everything this automation has done, without leaving the editor.

        Opened in the sideframe rather than a modal: a list of runs is
        something you read *against* the workflow beside it — this step failed,
        which node is that — and a modal covers the very thing you are
        comparing it with.

        Filtered to this automation, because the unfiltered list is every run
        of every automation on the site and the first thing anyone would do
        with it is narrow it to the one they are looking at.
        """
        automation = getattr(automation_content, "automation", None)
        if automation is None or not self.request.user.has_perm("djangocms_automation.view_automationinstance"):
            return
        url = (
            reverse("admin:djangocms_automation_automationinstance_changelist")
            + f"?automation_content__automation__id__exact={automation.pk}"
        )
        menu.add_sideframe_item(_("Past Runs"), url)

    def populate_trigger_menu(self, menu, automation_content, editable=True):
        """The triggers, and whether any of them can be opened.

        :param editable: Whether this version's flows accept changes. False
            leaves every entry in place and none of them clickable — what
            triggers an automation has is worth reading on a published version
            too, and an entry that disappears reads as a bug rather than as a
            state you leave by making a new version.
        """
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
            if editable:
                menu.add_modal_item(_("Add Trigger"), url)
            else:
                menu.add_link_item(_("Add Trigger"), url="", disabled=True)

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
                if can_change and editable:
                    url = reverse("admin:djangocms_automation_automationtrigger_change", args=[trigger.pk])
                    menu.add_modal_item(label, url)
                else:
                    # Listed and not clickable: for somebody who may see
                    # triggers and not change them, and for anybody at all once
                    # the version is published.
                    menu.add_link_item(label, url="", disabled=True)

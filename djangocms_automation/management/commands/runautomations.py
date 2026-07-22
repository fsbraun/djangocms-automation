"""Cron entry point for the automation engine.

Run periodically (e.g. every minute)::

    * * * * * cd /path/to/project && python manage.py runautomations

Revives due pending/paused actions and fires due timer triggers.
"""

from django.core.management.base import BaseCommand

from djangocms_automation import engine


class Command(BaseCommand):
    help = "Revive pending/paused automation actions and fire due timer triggers."

    def handle(self, *args, **options):
        fired = engine.fire_due_timers()
        revived = engine.revive_pending()
        self.stdout.write(self.style.SUCCESS(f"Fired {fired} timer trigger(s), revived {revived} action(s)."))

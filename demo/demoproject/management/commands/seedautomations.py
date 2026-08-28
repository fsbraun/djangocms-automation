"""Seed the Phase 0 reference automations as real CMS plugin trees.

Idempotent: automations are matched by name, so running it twice changes
nothing. Run it, start the server and a worker, and every reference automation
is there to open in the editor.

``--scenario`` injects the failure conditions the reliability milestone is
supposed to survive, so they can be observed rather than only asserted in tests.
"""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.utils.timezone import now

import datetime

from cms.api import add_plugin
from cms.models import Placeholder

from djangocms_automation.instances import AutomationAction, AutomationInstance, PENDING, RUNNING
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
from djangocms_automation.queue import QueuedTask

from demoproject.models import Article, Order

LANGUAGE = "en"


class Command(BaseCommand):
    help = "Create the reference automations, demo content, and an admin user."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scenario",
            choices=[
                "killed-worker",
                "timeout",
                "enqueue-rejection",
                "duplicate-webhook",
                "timer-backlog",
            ],
            help="Inject a failure condition into the seeded data to watch recovery work.",
        )
        parser.add_argument("--username", default="admin")
        parser.add_argument("--password", default="admin")

    # -- helpers ----------------------------------------------------------

    def _user(self, username, password):
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username, defaults={"is_staff": True, "is_superuser": True, "email": ""}
        )
        if created:
            user.set_password(password)
            user.save()
            self.stdout.write(f"  created superuser {username!r} (password {password!r})")
        return user

    def _placeholder(self, content, slot):
        return Placeholder.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(AutomationContent),
            object_id=content.pk,
            slot=slot,
        )[0]

    def _automation(self, user, name, description):
        """Get or create an automation and its content. Returns (content, created)."""
        existing = Automation.objects.filter(name=name).first()
        if existing is not None:
            content = AutomationContent.admin_manager.filter(automation=existing).first()
            return content, False
        automation = Automation.objects.create(name=name, is_active=True)
        content = AutomationContent.objects.with_user(user).create(automation=automation, description=description)
        return content, True

    # -- reference automations -------------------------------------------

    def _nightly_digest(self, user):
        """Timer → Query Records → Send Email.

        Proves timer recurrence and catch-up, scheduler locking, and retention.
        """
        content, created = self._automation(
            user,
            "Nightly content digest",
            "Every day, email editors the articles changed in the last 24 hours.",
        )
        if not created:
            return "Nightly content digest (exists)"

        placeholder = self._placeholder(content, "start")
        AutomationTrigger.objects.create(
            automation_content=content,
            slot="start",
            type="timer",
            position=0,
            config={
                "scheduled_at": (now() + datetime.timedelta(minutes=1)).isoformat(),
                "recurrence_frequency": "daily",
                "recurrence_interval": 1,
            },
        )
        add_plugin(
            placeholder=placeholder,
            plugin_type="QueryModelAction",
            language=LANGUAGE,
            config={
                "model": "demoproject.Article",
                # Filter values are expressions, not literals: a bare word is a
                # data path. Quote a literal ('"true"') or leave filters out.
                "filters": {},
                "fields": "title,slug,updated",
                "order_by": "-updated",
                "limit": 50,
            },
        )
        add_plugin(
            placeholder=placeholder,
            plugin_type="MailAction",
            language=LANGUAGE,
            config={
                "subject": '"Daily content digest"',
                "body": "Recently updated: {{ title }} ({{ slug }})",
                "recipient_email": '"editors@example.com"',
            },
        )
        return "Nightly content digest"

    def _webhook_ingest(self, user):
        """Webhook → Create Record → Send Email.

        Proves idempotency, retry policy, dead-letter and replay.
        """
        content, created = self._automation(
            user,
            "Webhook order ingest",
            "Accept an order over HTTP, store it, and confirm it by email.",
        )
        if not created:
            return "Webhook order ingest (exists)"

        placeholder = self._placeholder(content, "start")
        AutomationTrigger.objects.create(
            automation_content=content,
            slot="start",
            type="webhook",
            position=0,
            config={},
        )
        add_plugin(
            placeholder=placeholder,
            plugin_type="CreateModelAction",
            language=LANGUAGE,
            config={
                "model": "demoproject.Order",
                "field_mapping": {
                    "reference": "reference",
                    "email": "email",
                    "total": "total",
                },
            },
        )
        add_plugin(
            placeholder=placeholder,
            plugin_type="MailAction",
            language=LANGUAGE,
            config={
                "subject": '"Order received"',
                "body": "Thanks — we received order {{ reference }}.",
                "recipient_email": "email",
            },
        )
        return "Webhook order ingest"

    # -- failure injection ------------------------------------------------

    def _inject(self, scenario):
        action = AutomationAction.objects.order_by("-id").first()

        if scenario == "killed-worker":
            if action is None:
                return "no action to strand; trigger an automation first"
            stale = now() - datetime.timedelta(hours=1)
            AutomationAction.objects.filter(pk=action.pk).update(
                state=RUNNING, finished=None, started=stale, heartbeat_at=stale, max_attempts=3
            )
            AutomationInstance.objects.filter(pk=action.automation_instance_id).update(status=RUNNING, finished=None)
            return f"action {action.pk} left RUNNING with a stale heartbeat — run `runautomations`"

        if scenario == "timeout":
            if action is None:
                return "no action to time out; trigger an automation first"
            AutomationAction.objects.filter(pk=action.pk).update(
                state=RUNNING,
                finished=None,
                started=now() - datetime.timedelta(minutes=30),
                heartbeat_at=now(),
                timeout_seconds=1,
            )
            AutomationInstance.objects.filter(pk=action.automation_instance_id).update(status=RUNNING, finished=None)
            return f"action {action.pk} running past its timeout — run `runautomations`"

        if scenario == "enqueue-rejection":
            AutomationAction.objects.filter(state=PENDING).update(paused_until=None)
            QueuedTask.objects.all().delete()
            return "queue emptied while actions remain PENDING — run `runautomations` to re-enqueue"

        if scenario == "duplicate-webhook":
            trigger = AutomationTrigger.objects.filter(type="webhook").first()
            if trigger is None:
                return "no webhook trigger seeded"
            key = f"DUP-{now():%Y%m%d%H%M%S}"
            payload = {"reference": key, "email": "customer@example.com", "total": "10.00"}
            for _ in range(2):
                trigger.trigger_execution(data=[payload], idempotency_key=key)
            count = AutomationInstance.objects.filter(idempotency_key=key).count()
            return f"webhook delivered twice with key {key} -> {count} instance(s) (expected 1)"

        if scenario == "timer-backlog":
            trigger = AutomationTrigger.objects.filter(type="timer").first()
            if trigger is None:
                return "no timer trigger seeded"
            config = dict(trigger.config)
            config["scheduled_at"] = (now() - datetime.timedelta(days=5)).isoformat()
            config["recurrence_frequency"] = "hourly"
            config.pop("last_fired", None)
            config.pop("fired_count", None)
            trigger.config = config
            trigger.save(update_fields=["config"])
            return "timer backdated 5 days — run `runautomations --catch-up 3` to watch bounded catch-up"

        return "unknown scenario"

    # -- entry point ------------------------------------------------------

    def handle(self, *args, **options):
        user = self._user(options["username"], options["password"])

        if not Article.objects.exists():
            for index in range(1, 6):
                Article.objects.create(
                    title=f"Demo article {index}", slug=f"demo-article-{index}", is_published=index % 2 == 1
                )
            self.stdout.write("  created 5 demo articles")
        if not Order.objects.exists():
            Order.objects.create(reference="SEED-1", email="customer@example.com", total="42.00")
            self.stdout.write("  created 1 demo order")

        self.stdout.write("Reference automations:")
        for builder in (self._nightly_digest, self._webhook_ingest):
            self.stdout.write(f"  - {builder(user)}")

        if options["scenario"]:
            self.stdout.write(f"Scenario {options['scenario']}: {self._inject(options['scenario'])}")

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded. Next: `python manage.py runserver` and, in another shell, `python manage.py runworker`."
            )
        )

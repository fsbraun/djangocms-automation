"""Seed the Phase 0 reference automations as real CMS plugin trees.

Idempotent: automations are matched by name, so running it twice changes
nothing. Run it, start the server and a worker, and every reference automation
is there to open in the editor.

``--scenario`` injects the failure conditions the reliability milestone is
supposed to survive, so they can be observed rather than only asserted in tests.
"""

import datetime

from cms.api import add_plugin
from cms.models import Placeholder
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.utils.timezone import now

from demoproject.models import Article, Lead, Order
from djangocms_automation.instances import PENDING, RUNNING, AutomationAction, AutomationInstance
from djangocms_automation.models import Automation, AutomationContent, AutomationTrigger
from djangocms_automation.queue import QueuedTask

LANGUAGE = "en"
#: Answers locally, so every reference automation runs without an API key.
DUMMY_MODEL = "dummy/echo"


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
            intent="Find recently changed articles",
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
            intent="Send the daily digest",
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
            intent="Store the order",
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
            intent="Confirm the order",
            config={
                "subject": '"Order received"',
                "body": "Thanks — we received order {{ reference }}.",
                "recipient_email": "email",
            },
        )
        return "Webhook order ingest"

    def _contact_form(self, user):
        """Form submission → Ask a Model → If/Then/Else → Send Email.

        Proves that a model's judgement can pick the path: the step classifies
        what was written, and the conditional after it routes on the answer
        rather than on anything the sender chose.

        The trigger is of type *Form Submission*; point a djangocms-form-builder
        form at it (its *Trigger automation* action) to feed it from the site,
        or call ``trigger_execution`` with the same fields to try it now.
        """
        content, created = self._automation(
            user,
            "Intelligent contact form",
            "Read what somebody wrote in, decide what it is about, and reply accordingly.",
        )
        if not created:
            return "Intelligent contact form (exists)"

        placeholder = self._placeholder(content, "start")
        AutomationTrigger.objects.create(
            automation_content=content,
            slot="start",
            type="form_submission",
            position=0,
            config={
                # The fields djangocms-form-builder submits, plus who sent
                # them. Give a form the *Trigger automation* action and point
                # it here; the names have to agree.
                "data_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Who wrote in"},
                        "email": {"type": "string", "format": "email"},
                        "message": {"type": "string", "description": "What they said"},
                        # Set from the signed-in user, never from the form.
                        "user_id": {"type": ["integer", "null"], "description": "Who submitted it"},
                    },
                    "required": ["email", "message"],
                    "additionalProperties": False,
                }
            },
        )
        add_plugin(
            placeholder=placeholder,
            plugin_type="AIStep",
            language=LANGUAGE,
            intent="Classify the message",
            config={
                "model": DUMMY_MODEL,
                "prompt": (
                    "Decide whether this message is about billing or about support, "
                    "and answer with that one word.\n\n{{ message }}\n\n"
                    '!json {"topic": "billing"}'
                ),
                "output_schema": {
                    "type": "object",
                    "properties": {"topic": {"type": "string", "enum": ["billing", "support"]}},
                    "required": ["topic"],
                    "additionalProperties": False,
                },
            },
        )
        branch = add_plugin(
            placeholder=placeholder,
            plugin_type="AutomationIf",
            language=LANGUAGE,
            intent="Route the message",
            # Both sides are expressions, so the literal is quoted — bare
            # ``billing`` would be read as a path into the data.
            condition={"logic": "and", "conditions": [{"field": "topic", "operator": "==", "value": "'billing'"}]},
        )
        for child_type, recipient, subject in (
            ("ThenPlugin", '"billing@example.com"', '"A billing question came in"'),
            ("ElsePlugin", '"support@example.com"', '"A support question came in"'),
        ):
            path = add_plugin(placeholder=placeholder, plugin_type=child_type, language=LANGUAGE, target=branch)
            add_plugin(
                placeholder=placeholder,
                plugin_type="MailAction",
                language=LANGUAGE,
                target=path,
                intent="Forward the message",
                config={
                    "subject": subject,
                    "body": "It reads as {{ topic }}. Forwarded for a reply.",
                    "recipient_email": recipient,
                },
            )
        return "Intelligent contact form"

    def _editorial_review(self, user):
        """Ask a Model, with Update Records as a tool behind an approval gate.

        Proves the part of an agent worth being careful about: the model writes
        the change, a person sees the arguments it chose, and nothing is written
        until they say so.
        """
        content, created = self._automation(
            user,
            "Editorial AI review",
            "Draft a fresh title for an article; nothing is written until an editor approves.",
        )
        if not created:
            return "Editorial AI review (exists)"

        placeholder = self._placeholder(content, "start")
        AutomationTrigger.objects.create(automation_content=content, slot="start", type="click", position=0, config={})
        # Started by hand from the admin, so it arrives with no data at all —
        # the article to work on has to be fetched rather than assumed.
        add_plugin(
            placeholder=placeholder,
            plugin_type="QueryModelAction",
            language=LANGUAGE,
            intent="Find the latest article",
            config={
                "model": "demoproject.Article",
                "filters": {},
                "fields": "slug,title",
                "order_by": "-updated",
                "limit": 1,
            },
        )
        step = add_plugin(
            placeholder=placeholder,
            plugin_type="AIStep",
            language=LANGUAGE,
            intent="Draft a clearer title",
            config={
                "model": DUMMY_MODEL,
                "prompt": (
                    "Retitle {{ title }} so it reads better.\n\n"
                    '!call retitle_article {"field_mapping": {"title": "A clearer title"}}'
                ),
            },
        )
        add_plugin(
            placeholder=placeholder,
            plugin_type="UpdateModelAction",
            language=LANGUAGE,
            target=step,
            intent="Retitle the article",
            tool_name="retitle_article",
            tool_description=(
                "Rewrite an article's title. Use this once you have a title you are happy with. It cannot be undone."
            ),
            # The model writes the mapping; *which* article stays bound to the
            # automation. Note that exposing a mapping exposes all of it — the
            # model supplies the whole thing, so anything the editor wants
            # decided rather than written belongs in a field of its own or a
            # step of its own.
            exposed_fields=["field_mapping"],
            config={
                "model": "demoproject.Article",
                "filters": {"slug": "slug"},
            },
        )
        return "Editorial AI review"

    def _lead_qualification(self, user):
        """Ask a Model → Update Records → If/Then/Else → Send Email.

        Proves the ordinary shape of this: a model judges, the judgement is
        stored on the record, and the flow after it is drawn rather than
        decided by anybody clever.
        """
        content, created = self._automation(
            user,
            "Lead qualification",
            "Score an incoming lead, record the score, and tell sales about the good ones.",
        )
        if not created:
            return "Lead qualification (exists)"

        placeholder = self._placeholder(content, "start")
        AutomationTrigger.objects.create(
            automation_content=content,
            slot="start",
            type="code",
            position=0,
            config={
                "data_schema": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "format": "email"},
                        "company": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["email"],
                    "additionalProperties": False,
                }
            },
        )
        add_plugin(
            placeholder=placeholder,
            plugin_type="AIStep",
            language=LANGUAGE,
            intent="Score the lead",
            config={
                "model": DUMMY_MODEL,
                "prompt": (
                    "Score this lead hot, warm or cold from what they wrote, and give back "
                    "the address it came from.\n\n"
                    "{{ email }} at {{ company }}: {{ message }}\n\n"
                    '!json {"score": "hot", "email": "{{ email }}", "company": "{{ company }}"}'
                ),
                # The answer *becomes* the automation's data, so anything a
                # later step needs has to be part of it — the address included,
                # or the update below would have nothing to match on.
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "score": {"type": "string", "enum": ["hot", "warm", "cold"]},
                        "email": {"type": "string", "format": "email"},
                        # Carried only because the email below says it. Anything
                        # left out of the answer is gone by the time it renders.
                        "company": {"type": "string"},
                    },
                    # Every field, because a provider enforcing a schema insists
                    # on it. A field that need not be answered says so by
                    # allowing null, not by being left out of this list.
                    "required": ["score", "email", "company"],
                    "additionalProperties": False,
                },
            },
        )
        add_plugin(
            placeholder=placeholder,
            plugin_type="UpdateModelAction",
            language=LANGUAGE,
            intent="Record the lead score",
            config={
                "model": "demoproject.Lead",
                "filters": {"email": "email"},
                "field_mapping": {"score": "score"},
            },
        )
        branch = add_plugin(
            placeholder=placeholder,
            plugin_type="AutomationIf",
            language=LANGUAGE,
            intent="Select hot leads",
            condition={"logic": "and", "conditions": [{"field": "score", "operator": "==", "value": "'hot'"}]},
        )
        path = add_plugin(placeholder=placeholder, plugin_type="ThenPlugin", language=LANGUAGE, target=branch)
        add_plugin(
            placeholder=placeholder,
            plugin_type="MailAction",
            language=LANGUAGE,
            target=path,
            intent="Notify the sales team",
            config={
                "subject": '"A hot lead just came in"',
                "body": "{{ company }} scored {{ score }}. Call them.",
                "recipient_email": '"sales@example.com"',
            },
        )
        return "Lead qualification"

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
        if not Lead.objects.exists():
            Lead.objects.create(
                name="Ada Lovelace",
                email="ada@example.com",
                company="Analytical Engines",
                message="We need this for 200 seats by Q3.",
            )
            self.stdout.write("  created 1 demo lead")

        self.stdout.write("Reference automations:")
        for builder in (
            self._nightly_digest,
            self._webhook_ingest,
            self._contact_form,
            self._editorial_review,
            self._lead_qualification,
        ):
            self.stdout.write(f"  - {builder(user)}")

        if options["scenario"]:
            self.stdout.write(f"Scenario {options['scenario']}: {self._inject(options['scenario'])}")

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded. Next: `python manage.py runserver` and, in another shell, `python manage.py runworker`."
            )
        )

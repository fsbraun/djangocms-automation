import json

import django
from cms.admin.utils import ChangeListActionsMixin, GrouperModelAdmin
from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.options import IS_POPUP_VAR
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, mark_safe
from django.utils.translation import gettext_lazy as _

from . import engine
from .forms import AutomationTriggerAdminForm, RunNowForm
from .instances import (
    FAILED,
    PENDING,
    RUNNING,
    AutomationAction,
    AutomationInstance,
    DeadLetter,
    SchedulerLock,
)
from .models import APIKey, Automation, AutomationContent, AutomationTrigger
from .queue import QueuedTask
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
        "state",
        "attempt_count",
        "max_attempts",
        "re_entry_count",
        "dead_lettered",
        "message",
        "requires_interaction",
        "interaction_user",
        "interaction_group",
        "history",
        "conversation",
        "created",
        "started",
        "finished",
    )
    readonly_fields = (
        "state",
        "attempt_count",
        "re_entry_count",
        "dead_lettered",
        "message",
        "history",
        "conversation",
        "created",
        "started",
        "finished",
    )
    can_delete = False

    def get_fields(self, request, obj=None):
        """Drop the conversation column when this run held none.

        Asked of the *run* rather than of the installed apps: a project without
        the AI app never has one, and a project with it still runs plenty of
        automations that do not involve a model. Either way an always-empty
        column is a column that teaches the reader to ignore that part of the
        row.
        """
        fields = list(super().get_fields(request, obj))
        talked = obj is not None and obj.automationaction_set.filter(scratch__has_key="messages").exists()
        return fields if talked else [name for name in fields if name != "conversation"]

    @admin.display(description=_("History"))
    def history(self, obj):
        """Every state this action moved through, and what it cost to get there."""
        if not obj.pk:
            return "—"
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:djangocms_automation_action_history", args=[obj.pk]),
            _("%(count)d step(s)") % {"count": obj.events.count()},
        )

    @admin.display(description=_("Conversation"))
    def conversation(self, obj):
        """A way in to what a model was asked and what it said back.

        Only for a step that held a conversation. Every action carries a
        ``scratch``; only an AI step keeps messages in it, and a column of
        dashes down every other row would be noise.
        """
        if not obj.pk or not isinstance(obj.scratch, dict) or not obj.scratch.get("messages"):
            return "—"
        turns = obj.scratch.get("turn") or 0
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:djangocms_automation_conversation", args=[obj.pk]),
            _("Read (%(turns)d turn(s))") % {"turns": turns},
        )


@admin.register(AutomationInstance)
class AutomationInstanceAdmin(admin.ModelAdmin):
    list_display = ("id", "reference", "automation", "status", "is_success", "created", "updated")
    # Both open the run. Django links only the first column by default, which
    # would leave the hash as the one thing on the row you cannot click.
    list_display_links = ("id", "reference")
    list_filter = ("status", "automation_content__automation", "created")
    search_fields = ("key", "automation_content__automation__name")
    actions = ("cancel_instances",)

    class Media:
        css = {"all": ("djangocms_automation/css/admin_panels.css",)}

    @admin.display(description=_("Automation"), ordering="automation_content__automation__name")
    def automation(self, obj):
        """Which automation this run belongs to.

        A method rather than ``automation_content__automation`` in
        ``list_display``: Django resolves the path far enough to label the
        column and then fetches it with ``getattr(instance, "automation")``,
        which an instance has no such attribute for. The changelist raised
        ``AttributeError`` on every request.
        """
        content = getattr(obj, "automation_content", None)
        automation = getattr(content, "automation", None)
        return getattr(automation, "name", "") or "—"

    @admin.display(description=_("Hash"), ordering="key")
    def reference(self, obj):
        """The run's own name, as something to carry elsewhere.

        Beside the id rather than instead of it. The id is what people say
        out loud and what the URL carries; the hash is what survives leaving
        this database — a log line, a support ticket, a message to a colleague
        — where two deployments will happily disagree about run 17.

        Shortened the way a commit is: enough to recognise and to search for,
        with the whole of it on the run's own page.
        """
        if not obj.key:
            return format_html('<span class="automation-hash">#{}</span>', obj.pk)
        return format_html('<span class="automation-hash" title="{}">{}</span>', obj.key, obj.key[:12])

    @admin.display(description=_("Unique hash"))
    def hash(self, obj):
        """The whole hash, where there is room for it.

        Selectable rather than truncated: the reason to open this page for the
        hash is to copy it somewhere, and half a hash is no use for that.
        """
        if not obj.key:
            return "—"
        return format_html('<span class="automation-hash">{}</span>', obj.key)

    @admin.action(description=_("Cancel selected executions"), permissions=["change"])
    def cancel_instances(self, request, queryset):
        """Stop the selected runs and every unfinished action inside them."""
        canceled = sum(engine.cancel_instance(instance.pk) for instance in queryset)
        self.message_user(
            request,
            _("Canceled %(count)d action(s).") % {"count": canceled},
            level=messages.SUCCESS if canceled else messages.INFO,
        )

    readonly_fields = ("hash", "automation", "created", "updated", "data_display", "error_message_display")
    inlines = [AutomationActionInline]
    fieldsets = (
        (None, {"fields": (("hash", "automation"), ("created", "updated"))}),
        # Closed until asked for: a run's payload is the longest thing on the
        # page and the least often the reason for opening it.
        #
        # The class is ``collapse`` and only that — ``AdminField.is_collapsible``
        # tests ``"collapse" in self.classes``, so ``collapsed`` and
        # ``collapsable`` name nothing and the fieldset stays open. Django
        # renders it as ``<details>``, which costs no JavaScript and opens
        # itself when a field inside has an error.
        (
            _("Input data and results"),
            {
                "fields": ("data_display", "error_message_display"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Initial data"))
    def data_display(self, obj):
        """Display JSON data in a formatted, readable way."""
        if obj.data:
            formatted_json = json.dumps(obj.data, indent=2, ensure_ascii=False)
            return format_html("<pre>{}</pre>", formatted_json)
        return "-"

    @admin.display(description=_("Error messages"))
    def error_message_display(self, obj):
        """Display error messages from failed actions."""
        errors = obj.automationaction_set.filter(state=FAILED).values_list("result", flat=True)
        if errors:
            messages = [
                format_html(
                    "<details><summary>{}</summary><pre>{}</pre></details>",
                    message.get("error", _("No error message provided.")),
                    message.get("traceback", _("No traceback available.")),
                )
                for message in errors
            ]
            return mark_safe("\n".join(messages))
        return "-"

    @admin.display(description=_("Success"), boolean=True)
    def is_success(self, obj):
        """Indicate if the instance has failed actions."""
        if obj.automationaction_set.filter(state__in=(RUNNING, PENDING)).exists():
            return None  # Still running
        return not obj.automationaction_set.filter(state=FAILED).exists()

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related("automationaction_set")
            .select_related("automation_content", "automation_content__automation")
        )

    def get_urls(self):
        custom = [
            path(
                "open-tasks/",
                self.admin_site.admin_view(self.open_tasks_view),
                name="djangocms_automation_open_tasks",
            ),
            path(
                "open-tasks/<int:action_id>/resume/",
                self.admin_site.admin_view(self.resume_action_view),
                name="djangocms_automation_resume_action",
            ),
            path(
                "conversation/<int:action_id>/",
                self.admin_site.admin_view(self.conversation_view),
                name="djangocms_automation_conversation",
            ),
            path(
                "history/<int:action_id>/",
                self.admin_site.admin_view(self.action_history_view),
                name="djangocms_automation_action_history",
            ),
        ]
        return custom + super().get_urls()

    def open_tasks_view(self, request):
        """List actions waiting for interaction by the current user."""
        tasks = AutomationAction.get_open_tasks(request.user)
        context = {
            **self.admin_site.each_context(request),
            "title": _("Open tasks"),
            "tasks": tasks,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "djangocms_automation/admin/open_tasks.html", context)

    def action_history_view(self, request, action_id):
        """Every state one action moved through, in order.

        This used to be a changelist over every event in the database, which
        answered a question nobody asks — "show me all the actions that ever
        failed, across every run". What people want is the history of the run
        in front of them, so it lives on the action instead.
        """
        if not request.user.has_perm("djangocms_automation.view_automationinstance"):
            raise PermissionDenied
        action = get_object_or_404(AutomationAction.objects.select_related("automation_instance"), pk=action_id)
        events = list(action.events.order_by("created", "pk"))
        # How long each state lasted, rather than only when it began. "Ran for
        # four minutes" and "sat in the queue for four minutes" are the same
        # two timestamps and completely different problems, and reading them
        # off a column of clock times is work a page can do for you.
        previous = None
        for event in events:
            event.since = _span(event.created - previous.created) if previous else ""
            previous = event
        context = {
            **self.admin_site.each_context(request),
            "title": _("History"),
            "action": action,
            **_about(action),
            "events": events,
            # The two spans worth separating: waiting to start, and running.
            "queued_for": _span(action.started - action.created) if action.started else "",
            "ran_for": _span(action.finished - action.started) if action.finished and action.started else "",
            "parent": action.parent,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "djangocms_automation/admin/action_history.html", context)

    def conversation_view(self, request, action_id):
        """What a model was asked and what it said back, turn by turn.

        A run that went wrong is usually a prompt that read differently than it
        looked, or an answer nobody saw. Both are here and nowhere else — the
        conversation lives in the action's ``scratch``, which no admin page
        renders, so reading it has meant a Django shell.

        Read-only, and gated on being able to view a run at all: this is the
        run's data in its most complete form, including whatever a person put
        into the trigger.
        """
        if not request.user.has_perm("djangocms_automation.view_automationinstance"):
            raise PermissionDenied
        action = get_object_or_404(AutomationAction, pk=action_id)
        scratch = action.scratch if isinstance(action.scratch, dict) else {}
        answer_format = _answer_format(action)
        context = {
            **self.admin_site.each_context(request),
            "title": _("Conversation"),
            "action": action,
            **_about(action),
            "messages_": [_readable(message, answer_format) for message in scratch.get("messages") or []],
            "turn": scratch.get("turn") or 0,
            "tool_calls": scratch.get("tool_calls") or 0,
            "usage": scratch.get("usage") or {},
            # Markdown asked for and not rendered has two causes that look the
            # same on the page: the extra is missing, or the step no longer
            # says Markdown. Only the first is worth a notice, and only once.
            #
            # Gated here rather than on ``DEBUG`` in the template, because the
            # template has no such variable: the debug context processor sets
            # ``debug``, lowercase, and only when the client address is in
            # ``INTERNAL_IPS`` — which most projects never set. A condition on
            # ``DEBUG`` in a template is quietly always false.
            "markdown_missing": (settings.DEBUG and answer_format == "markdown" and not _can_render_markdown()),
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "djangocms_automation/admin/conversation.html", context)

    def resume_action_view(self, request, action_id):
        """Resume a waiting action for the current user."""
        redirect_url = reverse("admin:djangocms_automation_open_tasks")
        if request.method != "POST":
            return HttpResponseRedirect(redirect_url)
        try:
            engine.resume_action(action_id, request.user)
        except (AutomationAction.DoesNotExist, ValueError, PermissionError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, _("Task resumed."), level=messages.SUCCESS)
        return HttpResponseRedirect(redirect_url)


def _span(delta) -> str:
    """A duration at the precision a person reads it in.

    ``0:00:00.006830`` is technically the answer and practically a puzzle. The
    interesting comparisons here are between milliseconds and minutes, so the
    unit changes with the magnitude and everything below it is dropped.
    """
    if delta is None:
        return ""
    seconds = delta.total_seconds()
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        minutes, rest = divmod(int(seconds), 60)
        return f"{minutes} min {rest} s"
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours} h {rest // 60} min"


def _about(action) -> dict:
    """Which automation, which run, which step.

    A page that opens on a conversation or a list of state changes is missing
    the thing a reader needs first: what this belongs to. Someone arrives here
    from a link, a bookmark or a colleague, and "which of our automations is
    this" should not be a click away.
    """
    instance = action.automation_instance
    content = getattr(instance, "automation_content", None)
    return {
        # Django 6.1 replaced the breadcrumb <div> with an ordered list. The
        # panels follow whichever the installed admin uses, so they sit in the
        # bar the same way every other page does on both.
        "modern_breadcrumbs": django.VERSION >= (6, 1),
        "step": _step_name(action),
        "automation": getattr(content, "automation", None),
        "instance": instance,
    }


def _answer_format(action) -> str:
    """What the step asked the model to write in *when it ran*.

    Read from the run's own state, because that is the only place it is a
    fact. A plugin is editable and a transcript is not: switching a step to
    Markdown next week must not change how last week's answer is read back.

    The plugin is consulted only for a run recorded before this was kept —
    there the current setting is the sole remaining clue, and a plain answer
    rendered as Markdown is a paragraph either way.
    """
    scratch = action.scratch if isinstance(action.scratch, dict) else {}
    if "answer_format" in scratch:
        return str(scratch.get("answer_format") or "")
    try:
        plugins = engine.build_plugin_map(action.automation_instance.automation_content_id)
        plugin = plugins.get(action.plugin_ptr)
        return str((getattr(plugin, "config", None) or {}).get("answer_format") or "")
    except Exception:  # noqa: BLE001 — an unreadable tree only means plainer output
        return ""


def _step_name(action, plugins=None) -> str:
    """Which step this is, in the words the editor sees.

    A page about one action that never says which action is a page about a
    UUID. The plugin tree is the only thing that knows, so it is asked; a
    plugin deleted since the run is not a reason to fail the page.

    :param plugins: A prebuilt plugin map, for a caller naming many actions at
        once. Building it per row would put a tree walk behind every line of
        the table.
    """
    name = action.step_name(plugins)
    if not name:
        return ""
    try:
        plugin = (plugins or engine.build_plugin_map(action.automation_instance.automation_content_id)).get(
            action.plugin_ptr
        )
        label = str(getattr(plugin, "comment", "") or "")
    except Exception:  # noqa: BLE001 — a missing comment is not worth failing a page for
        label = ""
    return f"{name} — {label}" if label else name


def _rendered(text: str, answer_format: str):
    """A model's words, prepared for a page, or ``None`` to show them as text.

    Only Markdown is turned into HTML, and only through a sanitiser. HTML that
    a model was *asked* for is deliberately not rendered: an editor who wants
    HTML wants to read what the model wrote, and showing it as a page instead
    would both hide the markup they are checking and hand a model's output
    straight to the browser.

    Nothing is marked safe that has not been through ``nh3``. A model's answer
    can be steered by whatever arrived in the trigger — an inbound email, a
    form — so this is untrusted text in the ordinary sense, on a page that
    someone with admin rights is reading.
    """
    if answer_format != "markdown" or not text.strip():
        return None
    if not _can_render_markdown():
        # Without both halves it stays text. Escaped and readable beats
        # rendered and unsanitised — and the page says why, because "my
        # Markdown is showing as text" has two causes that look identical.
        return None
    import markdown
    import nh3

    return mark_safe(nh3.clean(markdown.markdown(text, extensions=["fenced_code", "tables"])))


def _can_render_markdown() -> bool:
    """Whether both halves of the Markdown extra are installed."""
    try:
        import markdown  # noqa: F401
        import nh3  # noqa: F401
    except ImportError:
        return False
    return True


def _pairs(text: str, answer_format: str):
    """A flat JSON answer as the fields it is, or ``None`` if it is not one.

    A step with an output shape answers with an object, and the whole point of
    the shape is that it has named fields. Printing that back as a line of JSON
    makes the reader parse what the schema already separated.
    """
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    if any(isinstance(value, (dict, list)) for value in parsed.values()):
        return None  # not flat; the JSON is the clearer rendering of a nested answer
    return [
        {"key": key, "text": "" if value is None else str(value), "html": _rendered(str(value), answer_format)}
        for key, value in parsed.items()
    ]


def _readable(message: dict, answer_format: str = "") -> dict:
    """One stored message, in the shape the template reads.

    The arguments a model sent are a JSON string on the wire. Re-indented here
    because the point of the page is reading them, and a single line of dense
    JSON is the thing that sends people back to the shell.
    """
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments")
        try:
            arguments = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            arguments = str(raw)
        calls.append({"name": function.get("name", ""), "arguments": arguments})
    content = message.get("content") or ""
    return {
        "role": message.get("role", ""),
        "content": content,
        # An answer under a shape reads as its fields; anything else as itself.
        "pairs": _pairs(content, answer_format) if message.get("role") == "assistant" else None,
        "html": _rendered(content, answer_format) if message.get("role") == "assistant" else None,
        "tool_call_id": message.get("tool_call_id") or "",
        "calls": calls,
    }


@admin.register(DeadLetter)
class DeadLetterAdmin(admin.ModelAdmin):
    """The dead-letter queue: actions that exhausted their attempts.

    Read-only by design. The only operation is replay, which creates a new
    action linked back to the original and never edits execution history.
    """

    list_display = (
        "id",
        "automation_instance",
        "plugin_ptr",
        "attempt_count",
        "message",
        "dead_lettered_at",
        "replay_count",
    )
    list_filter = ("dead_lettered_at", "automation_instance__automation_content__automation")
    search_fields = ("automation_instance__key", "message", "error_type")
    readonly_fields = (
        "automation_instance",
        "plugin_ptr",
        "state",
        "attempt_count",
        "max_attempts",
        "re_entry_count",
        "message",
        "error_type",
        "error_detail",
        "input_data",
        "result",
        "started",
        "finished",
        "dead_lettered_at",
        "replayed_from",
    )
    actions = ("replay_actions",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(dead_lettered=True)
            .select_related("automation_instance")
            .prefetch_related("replays")
        )

    @admin.display(description=_("Replays"))
    def replay_count(self, obj):
        return obj.replays.count()

    def has_replay_permission(self, request):
        """Authorize the replay action.

        The dead-letter queue is read-only, so it has no change permission of
        its own to borrow. Replay re-runs an action with real side effects —
        sending mail, writing records — so it is gated on the permission to
        change executions rather than on the ability to view this list.
        """
        return request.user.has_perm("djangocms_automation.change_automationinstance")

    @admin.action(description=_("Replay selected actions"), permissions=["replay"])
    def replay_actions(self, request, queryset):
        """Re-run each selected action with the input its failed attempt saw."""
        replayed = 0
        for action in queryset:
            if engine.replay_action(action.pk) is not None:
                replayed += 1
        self.message_user(
            request,
            _("Replayed %(count)d action(s).") % {"count": replayed},
            level=messages.SUCCESS if replayed else messages.WARNING,
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(QueuedTask)
class QueuedTaskAdmin(admin.ModelAdmin):
    """The durable queue, for watching backlog and inspecting worker failures.

    Read-only: a task row is worker state, not something to hand-edit. To make
    failed work run again, replay the automation action it belongs to.
    """

    list_display = ("id", "task_path", "state", "queue_name", "attempts", "enqueued_at", "worker_id")
    list_filter = ("state", "queue_name", "enqueued_at")
    search_fields = ("task_path", "result_id", "worker_id")
    readonly_fields = (
        "result_id",
        "task_path",
        "queue_name",
        "priority",
        "args",
        "kwargs",
        "state",
        "run_after",
        "enqueued_at",
        "started_at",
        "finished_at",
        "worker_id",
        "claimed_until",
        "attempts",
        "error",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SchedulerLock)
class SchedulerLockAdmin(admin.ModelAdmin):
    """Visibility into which scheduler holds the tick, for debugging.

    Hidden from the index: it is one row of internal machinery, and a menu
    entry for a singleton earns its place only when something has gone wrong.
    The page is still there for whoever is debugging a stuck scheduler and
    knows to go looking.
    """

    def get_model_perms(self, request):  # Hides from admin index/app list
        return {}

    list_display = ("name", "holder", "locked_until")
    readonly_fields = ("name", "holder", "locked_until")

    def has_add_permission(self, request):
        return False


# Neither event model has an admin of its own any more.
#
# A run's own transitions — started, finished, canceled — are already the
# instance's ``status``, ``created`` and ``finished``, so a second place to read
# them said nothing new, and said nothing at all for a run still in flight.
#
# An action's transitions are worth keeping, but not as a changelist: nobody
# looks for "every action that ever failed, across every run". They look at one
# run. So they are a panel on the action instead, beside its conversation. The
# events themselves are still recorded exactly as before — this changed where
# they are read, not whether they exist.


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
        (
            None,
            {
                "fields": (
                    "name",
                    (
                        "service",
                        "api_key",
                    ),
                    "masked_key",
                    "is_active",
                )
            },
        ),
        ("Details", {"fields": ("description", ("created", "updated")), "classes": ("collapse",)}),
    )

    @admin.display(
        description=_("Service"),
        ordering="service",
    )
    def service_display(self, obj):
        """Display the human-readable service name."""
        return obj.get_service_display()

    @admin.display(description="Masked Key")
    def masked_key(self, obj):
        """Display a masked version of the API key for security."""
        if obj.api_key:
            key_length = len(obj.api_key)
            if key_length > 8:
                return f"{obj.api_key[:4]}{'*' * (key_length - 8)}{obj.api_key[-4:]}"
            return "*" * key_length
        return "-"


@admin.register(AutomationTrigger)
class AutomationTriggerAdmin(ChangeListActionsMixin, admin.ModelAdmin):
    """Admin for AutomationTrigger: add/change views available, hidden from index."""

    #: Query parameters present when this admin is opened from the automation
    #: editor rather than navigated to directly. ``language`` comes from the
    #: ``{% render_model_block %}`` edit link on a trigger node, ``cms_path``
    #: from toolbar modal items, ``edit_fields`` from field-level frontend
    #: editing, and ``automation_content`` from the "Add Trigger" link.
    cms_modal_markers = ("cms_path", "edit_fields", "language", "automation_content")

    name = _("Trigger")
    form = AutomationTriggerAdminForm
    list_display = (
        "__str__",
        "type",
        "slot",
    )
    list_editable = ("slot",)  # Makes 'slot' editable in changelist with save button
    ordering = (
        "automation_content",
        "position",
    )
    list_filter = ("automation_content",)

    class Media:
        js = ("djangocms_automation/js/trigger_type_change.js",)
        css = {"all": ("djangocms_automation/css/trigger_admin.css",)}

    def _mark_as_popup(self, request):
        """Render as a popup when django CMS opened this view in its modal.

        django CMS appends ``_popup=1`` for toolbar modal items, but not for the
        frontend-editing links ``{% render_model_block %}`` builds around each
        trigger node — those are a plain admin change URL. Without the flag the
        full admin chrome (branding, sidebar, breadcrumbs) renders inside a small
        modal, and saving lands on the changelist rather than closing it.

        Setting the flag on the request rather than faking ``is_popup`` in the
        template context keeps Django as the single source of truth: it renders
        the popup layout, emits the hidden ``_popup`` input, and returns the
        popup response on save, which is what tells the modal it may close.
        """
        if IS_POPUP_VAR in request.GET or IS_POPUP_VAR in request.POST:
            return  # Already flagged by the toolbar.
        if not any(marker in request.GET for marker in self.cms_modal_markers):
            return  # A direct visit to the admin URL keeps the normal chrome.
        request.GET = request.GET.copy()
        request.GET[IS_POPUP_VAR] = "1"

    def add_view(self, request, form_url="", extra_context=None):
        self._mark_as_popup(request)
        return super().add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        self._mark_as_popup(request)
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        custom = [
            path(
                "run/",
                self.admin_site.admin_view(self.run_now_view),
                name="djangocms_automation_run_now",
            ),
        ]
        return custom + super().get_urls()

    def run_now_view(self, request):
        """Start a run by hand, from the toolbar.

        Every other entry point needs something outside the editor — a webhook
        delivery, a cron tick, a form submission, a Django shell. So the
        natural moment to try an automation, just after building it, was the
        one moment there was no way to.

        Creating an instance is what this does, so the permission asked for is
        the one for creating an instance. It is a real run against real data:
        mail is sent, records are written, and an approval gate waits for a
        person exactly as it would have done at three in the morning.
        """
        automation_content = get_object_or_404(
            AutomationContent.admin_manager, pk=request.GET.get("automation_content") or 0
        )
        if not request.user.has_perm("djangocms_automation.add_automationinstance"):
            raise PermissionDenied

        form = RunNowForm(request.POST or None, automation_content=automation_content)
        if request.method == "POST" and form.is_valid():
            trigger = form.cleaned_data["trigger"]
            instance = trigger.trigger_execution(data=form.cleaned_data["rows"])
            self.message_user(
                request,
                _("Started %(automation)s. Watch it under Execution Instances.") % {"automation": automation_content},
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(
                reverse("admin:djangocms_automation_automationinstance_change", args=[instance.pk])
            )

        self._mark_as_popup(request)
        context = {
            **self.admin_site.each_context(request),
            "title": _("Run now"),
            "form": form,
            "automation_content": automation_content,
            "opts": self.model._meta,
            "is_popup": True,
        }
        return TemplateResponse(request, "djangocms_automation/admin/run_now.html", context)

    @staticmethod
    def get_trigger(request, obj) -> tuple[forms.Form | None, bool]:
        trigger_type = request.POST.get("_trigger_type_change") if request.method == "POST" else None
        fallback = trigger_type or (obj.type if obj else request.GET.get("type") or "click")
        return trigger_registry.get(trigger_type or fallback), trigger_type is not None

    def get_fieldsets(self, request, obj=None):
        """Return fieldsets with dynamic config fields based on trigger type."""
        base_fieldsets = [
            (None, {"fields": ("automation_content", "type", "slot", "position")}),
        ]
        trigger_class, changed = self.get_trigger(request, obj)
        # Add config fieldset if trigger has config fields
        if trigger_class and not changed and trigger_class.declared_fields:
            # Get all config field names (with config_ prefix)
            base_fieldsets.append(
                (
                    trigger_class.name,
                    {
                        "fields": list(trigger_class.declared_fields.keys()),
                        "classes": ("collapse",),
                    },
                )
            )
        return base_fieldsets

    def get_form(self, request, obj=None, **kwargs):
        """Build a form class combining the admin form with the trigger's config fields.

        The composed class is passed through ``kwargs`` rather than assigned to
        ``self.form``. A ``ModelAdmin`` is instantiated once and shared by every
        request, so assigning to it lets two concurrent requests for different
        trigger types overwrite each other — one editing a timer could be served
        the webhook's configuration fields.
        """
        trigger_class, changed = self.get_trigger(request, obj)
        if trigger_class and not changed:
            form_class = type("FormWithTriggerConfig", (AutomationTriggerAdminForm, trigger_class), {})
        else:
            form_class = AutomationTriggerAdminForm

        # Mirror ModelAdmin.get_form: a declared field that is read-only must not
        # be rendered as an editable input. Django applies this to ``self.form``,
        # which is no longer the class being used, so apply it here instead.
        readonly = dict.fromkeys(
            name for name in self.get_readonly_fields(request, obj) if name in form_class.declared_fields
        )
        if readonly:
            form_class = type(form_class.__name__, (form_class,), readonly)

        kwargs["form"] = form_class
        form = super().get_form(request, obj, **kwargs)

        # Add localized confirmation message as data attribute
        if "type" in form.base_fields:
            form.base_fields["type"].widget.attrs["data-confirm-message"] = str(
                _(
                    "Changing the trigger type will reload the form with different configuration fields. "
                    "Current configuration will not be saved. Continue?"
                )
            )

        return form

    def save_model(self, request, obj, form, change):
        """Handle type changes during save."""
        # Check if this is a type change
        if "_trigger_type_change" in request.POST:
            new_type = request.POST.get("_trigger_type_change")
            if new_type:
                obj.type = new_type
                # Clear old config when changing type
                obj.config = {}

        super().save_model(request, obj, form, change)

    def response_change(self, request, obj):
        """Redirect to change form with new type after type change."""
        if "_trigger_type_change" in request.POST:
            # Redirect to same change form to reload with new fields
            from django.http import HttpResponseRedirect
            from django.urls import reverse

            url = reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_change", args=[obj.pk])
            return HttpResponseRedirect(url)
        return super().response_change(request, obj)

    def get_model_perms(self, request):  # Hides from admin index/app list
        return {}

    def __str__(self):
        return str(self.name)

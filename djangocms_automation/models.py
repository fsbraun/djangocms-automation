import uuid

from cms.models import CMSPlugin, Placeholder
from cms.models.fields import PlaceholderRelationField
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _

from .instances import (  # noqa F401
    CANCELED,
    COMPLETED,
    FAILED,
    PENDING,
    RUNNING,
    WAITING,
    AutomationAction,
    AutomationInstance,
    AutomationInstanceEvent,
    DeadLetter,
    SchedulerLock,
)
from .queue import QueuedTask  # noqa F401 — registers the durable queue model
from .services import service_registry
from .tool_mixin import ToolMixin
from .triggers import trigger_registry
from .utilities.conditions import evaluate as evaluate_condition


class AutomationContent(models.Model):
    """Container for all versioned content of an automation.

    Holds placeholders and triggers for potentially different versions of an automation.
    Exists only in the site's default language (not translatable).
    """

    automation = models.ForeignKey(
        "djangocms_automation.Automation", related_name="contents", on_delete=models.CASCADE
    )
    description = models.TextField()

    placeholders = PlaceholderRelationField()

    def get_title(self) -> str:
        """Get the automation's name.

        :returns: Name of the associated automation.
        :rtype: str
        """
        return self.automation.name

    def get_description(self) -> str:
        """Get the content description.

        :returns: Description text.
        :rtype: str
        """
        return self.description

    def __str__(self):
        return self.get_title()

    def get_template(self) -> None:
        """Get the template for rendering (not used).

        :returns: Always None.
        :rtype: None
        """
        return

    def get_placeholder_slots(self) -> list[str]:
        """Get slot names for all triggers.

        :returns: List of trigger slot identifiers.
        :rtype: list[str]
        """
        return list(self.triggers.values_list("slot", flat=True))


class Automation(models.Model):
    """Top-level automation workflow definition."""

    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("Automation")
        verbose_name_plural = _("Automations")


class AutomationTrigger(models.Model):
    """Entry point that initiates an automation workflow execution."""

    automation_content = models.ForeignKey(AutomationContent, related_name="triggers", on_delete=models.CASCADE)
    slot = models.SlugField(
        verbose_name=_("Slot"),
        help_text=_(
            "Unique identifier for this trigger within the automation content. Used, e.g., if it needs to be triggered by other automation."
        ),
        max_length=255,
    )
    type = models.CharField(max_length=100, default="code")
    config = models.JSONField(default=dict, editable=False)
    position = models.PositiveIntegerField(default=0, verbose_name="#")

    class Meta:
        verbose_name = _("Trigger")
        verbose_name_plural = _("Triggers")
        ordering = ["position"]
        constraints = [
            # The slot is how a trigger finds the placeholder holding its flow,
            # so two triggers sharing one within an automation is not a naming
            # nicety — renaming one onto the other's slot renames its
            # placeholder too, and ``.get(slot=…)`` then raises
            # ``MultipleObjectsReturned`` for both of them.
            #
            # A ``UniqueConstraint`` rather than ``unique_together`` because a
            # ModelForm validates constraints as well as uniqueness, so this
            # message reaches the editor instead of a database error reaching
            # the logs.
            models.UniqueConstraint(
                fields=["automation_content", "slot"],
                name="unique_trigger_slot_per_automation",
                violation_error_message=_(
                    "Another trigger on this automation already uses this slot. "
                    "The slot is how a trigger finds its own flow, so no two can share one."
                ),
            )
        ]

    def save(self, *args, **kwargs):
        """Save, keeping the trigger's placeholder named after it.

        A trigger's ``slot`` is how it finds the placeholder holding its flow,
        so renaming one without renaming the other leaves the plugins where
        nothing looks for them. The workflow still shows in the editor — the
        placeholder is intact — and every run fails with *has no placeholder
        and so no plugins to execute*, which does not sound like "you renamed
        something".

        Here rather than in the admin form because a rename is a rename: from
        the change view, a shell, a fixture or a data migration, the two names
        have to move together.
        """
        renamed_from = None
        if self.pk:
            renamed_from = type(self).objects.filter(pk=self.pk).values_list("slot", flat=True).first()
        super().save(*args, **kwargs)
        if renamed_from and renamed_from != self.slot:
            self.placeholders().filter(slot=renamed_from).update(slot=self.slot)

    def placeholders(self):
        """Every placeholder belonging to this trigger's automation."""
        return Placeholder.objects.get_for_obj(self.automation_content)

    def get_definition(self):
        """Get the trigger type definition from the registry.

        :returns: Trigger definition object or None if not found.
        """
        return trigger_registry.get(self.type)

    @property
    def data_schema(self) -> dict:
        """What this trigger accepts, as a JSON schema.

        Empty means it accepts anything. This is the trigger's public contract:
        what a webhook caller must send, and what another automation — or an
        agent calling it as a tool — has to be told before it can call this at
        all.
        """
        definition = self.get_definition()
        if definition is None:
            return {}
        return definition().get_data_schema(self.config)

    def trigger_execution(
        self,
        data: dict | None = None,
        start: bool = True,
        idempotency_key: str | None = None,
        parent_action=None,
    ) -> "AutomationInstance | None":
        """Create and optionally start an automation instance.

        :param data: Initial data dictionary for the automation.
        :param start: Whether to immediately enqueue the action for execution.
            Defaults to True.
        :param idempotency_key: Optional caller-supplied key. When provided,
            only one instance per (automation content × key) is created —
            duplicate calls become no-ops. Use this to safely retry webhook
            deliveries without double-execution.
        :param parent_action: The action starting this run, when one is — an
            agent's tool call. The new run reports back to it when it finishes.
        :returns: The instance created, or ``None`` when an idempotency key
            meant there was nothing to do.
        """
        from django.db import IntegrityError

        if (
            idempotency_key
            and AutomationInstance.objects.filter(
                automation_content=self.automation_content,
                idempotency_key=idempotency_key,
            ).exists()
        ):
            return  # Already executed for this key — idempotent no-op.

        try:
            placeholder = Placeholder.objects.get_for_obj(self.automation_content).get(slot=self.slot)
        except Placeholder.DoesNotExist:
            raise ValueError(
                f"Automation trigger '{self.slot}' has no placeholder and so no plugins to execute."
            ) from None
        plugin = placeholder.get_plugins().first()
        if plugin is None:
            # A misconfigured automation (no placeholder, or no plugin in it) must
            # report a diagnosable failure rather than raising AttributeError from
            # deep inside a webhook or a cron tick.
            raise ValueError(
                f"Automation trigger '{self.slot}' has no plugins to execute. "
                f"Add at least one plugin to the '{self.slot}' slot."
            )
        plugin, _ = plugin.get_plugin_instance()

        with transaction.atomic():
            try:
                instance = AutomationInstance.objects.create(
                    automation_content=self.automation_content,
                    data=data or [],
                    initial_data=data or [],
                    idempotency_key=idempotency_key,
                    parent_action=parent_action,
                )
            except IntegrityError:
                return None  # Race lost — another request created an instance for this key.
            action = AutomationAction.objects.create(
                previous=None,
                parent=None,
                automation_instance=instance,
                plugin_ptr=plugin.uuid,
                finished=None,
            )
        if start:
            from .engine import enqueue_action

            enqueue_action(action.pk, data=data)
        return instance

    def __str__(self):
        type = trigger_registry.get(self.type)
        return f"{self.slot.capitalize()} ({type.name if type else 'unknown'})"


class APIKey(models.Model):
    """Store named API keys for external services."""

    name = models.CharField(
        max_length=255,
        verbose_name=_("Name"),
        help_text=_("Descriptive name for this API key"),
    )
    service = models.CharField(
        max_length=100,
        verbose_name=_("Service"),
        help_text=_("The service this API key is for"),
    )
    api_key = models.CharField(
        max_length=500,
        verbose_name=_("API Key"),
        help_text=_("The API key or token"),
    )
    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
        help_text=_("Optional notes about this API key"),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
        help_text=_("Whether this API key is active"),
    )
    created = models.DateTimeField(auto_now_add=True, verbose_name=_("Created"))
    updated = models.DateTimeField(auto_now=True, verbose_name=_("Updated"))

    class Meta:
        verbose_name = _("Secret")
        verbose_name_plural = _("Secrets")
        ordering = ["service", "name"]
        indexes = [
            models.Index(fields=["service", "is_active"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_service_display()})"

    def get_service_display(self) -> str:
        """Get the human-readable service name.

        :returns: Display name of the service, or raw service identifier if not found.
        :rtype: str
        """
        service = service_registry.get(self.service)
        return service["name"] if service else self.service

    @classmethod
    def get_service_choices(cls) -> list[tuple[str, str]]:
        """Get available service choices for form fields.

        :returns: List of (value, label) tuples for service selection.
        :rtype: list[tuple[str, str]]
        """
        return service_registry.get_choices()


class AutomationPluginModel(CMSPlugin):
    """Base model for all automation plugins.

    Provides common fields (uuid, comment) and abstract methods for
    execution and action chaining that subclasses must implement.
    """

    class Meta:
        abstract = True

    uuid = models.UUIDField(
        editable=False,
        verbose_name=_("UUID"),
        default=uuid.uuid4,
    )
    comment = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Comment"),
        help_text=_("Optional comment about this automation step"),
    )

    def resume_reenters(self, action: AutomationAction) -> bool:
        """Whether resuming this action re-enters it instead of completing it.

        Resuming normally means "this step is done": the engine completes the
        waiting action and carries on. A node that pauses *before* doing its
        work — a tool call waiting for approval — needs the opposite, and is
        enqueued to run again instead.

        Asked per action rather than per class, because the same action can do
        both: *Wait for User* as a step in a flow is finished when somebody
        resumes it, and the very same action as a tool call has not started.
        """
        return False

    def scratch_for_replay(self, scratch: dict) -> dict:
        """What a replayed action needs to carry over from the one it replaces.

        A replacement is seeded from the failed attempt's *input*, which is
        enough for most nodes: what they were asked to do is in the data. It is
        not enough for a node whose instruction lives on itself — an agent's
        tool call is the request a model made, not a data row — and such a node
        replays as a blank without it.

        Nothing is carried by default. The failed node's working state is the
        state that failed, and a replay is a new attempt, not a resumption of
        the old one; a node opts in to the specific parts that identify *which*
        piece of work this is.
        """
        return {}

    def on_resume(self, action: AutomationAction, user, data: dict | None) -> None:
        """Record a person's decision, in the transaction that resumes.

        Called only for a node with :attr:`resume_reenters`, immediately before
        it is woken, and committed with it. A node that runs again on resume
        cannot infer consent from its own state: a crash between pausing and
        being recovered would look identical. It has to be written down.
        """

    def execute(self, action: AutomationAction, data: dict, single_step: bool = False, **kwargs):
        """Execute the plugin logic for the given action.

        :param action: The automation action being executed.
        :type action: AutomationAction
        :param data: Current automation data dictionary.
        :type data: dict
        :param single_step: If True, execute only this step without continuing.
        :type single_step: bool
        :raises NotImplementedError: Subclasses must implement this method.
        """
        raise NotImplementedError("Subclasses must implement the execute method.")

    @staticmethod
    def _uuid_of(plugin):
        """Get a plugin's uuid, downcasting it first if necessary."""
        if not hasattr(plugin, "uuid"):
            plugin, _unused = plugin.get_plugin_instance()
        return plugin.uuid

    def get_next_payload(self, action: AutomationAction, state: str, output, rows: list):
        """Decide what the actions created by :meth:`get_next_actions` receive.

        A completed node hands on its output; a node that fanned out passes its
        own input through, because every branch of a split starts from the same
        data. A loop is the exception — each iteration must receive the previous
        one's output, or it can never work towards its own exit — so it
        overrides this.
        """
        return output if state == COMPLETED else rows

    def get_next_actions(self, action: AutomationAction) -> list[AutomationAction]:
        """Determine and create the next action(s) in the workflow.

        :param action: The current completed action.
        :type action: AutomationAction
        :returns: List of newly created AutomationAction instances to execute next.
        :rtype: list[AutomationAction]
        """
        if action.state != COMPLETED:
            return []

        next_plugin = self.next_plugin_instance
        # Only create an action if the next plugin has a uuid (is an AutomationPluginModel)
        if next_plugin and hasattr(next_plugin, "uuid"):
            return [
                AutomationAction.objects.create(
                    previous=action,
                    parent=action.parent,
                    automation_instance=action.automation_instance,
                    plugin_ptr=next_plugin.uuid,
                    finished=None,
                )
            ]
        return []


class ConditionalPluginModel(AutomationPluginModel):
    """Plugin model for conditional branching based on evaluated expressions.

    On first execution the condition is evaluated against the automation
    data and an action for the first plugin of the matching "Yes"/"No"
    branch is created (the conditional itself goes ``WAITING``). Once the
    branch chain finishes, the conditional completes with the branch's
    output and the flow resumes after the conditional block.
    """

    question = models.CharField(
        max_length=255,
        verbose_name=_("Question"),
        blank=True,
        help_text=_(
            "The question this conditional answers, e.g., 'Is the user active?' It will be shown in the editor."
        ),
    )
    condition = models.JSONField(
        verbose_name=_("Condition"),
        help_text=_(
            "Condition to evaluate for this conditional to evaluate. Use double curly braces {{ }} for data attribute resolution, e.g. {{ first_name }}."
        ),
        default=dict,
    )

    no_yes_channel = _(
        'No "Yes" channel defined. The yes channel determines which actions will be executed if the condition is met. '
        'Please add a "Yes" branch to this conditional in the structure board.'
    )
    no_no_channel = _(
        'No "No" channel defined. The no channel determines which actions will be executed if the condition is not met. '
        'Please add a "No" branch to this conditional in the structure board.'
    )
    multiple_channels = _(
        'Both the "Yes" and "No" cannot be defined more than once. Please make sure only one branch is present '
        "for both of them for this conditional."
    )

    def messages(self) -> list[str]:
        """Get validation messages for this conditional.

        :returns: List of warning/error messages about missing or duplicate branches.
        :rtype: list[str]
        """
        messages = []
        yes_channels = [child for child in self.child_plugin_instances if child.plugin_type == "ThenPlugin"]
        no_channels = [child for child in self.child_plugin_instances if child.plugin_type == "ElsePlugin"]
        if len(yes_channels) == 0:
            messages.append(self.no_yes_channel)
        if len(no_channels) == 0:
            messages.append(self.no_no_channel)
        if len(yes_channels) > 1 or len(no_channels) > 1:
            messages.append(self.multiple_channels)
        return messages

    def _get_branch(self, condition_result: bool):
        """Get the branch container plugin for the condition outcome."""
        plugin_type = "ThenPlugin" if condition_result else "ElsePlugin"
        for child in self.child_plugin_instances or []:
            if child.plugin_type == plugin_type:
                return child
        return None

    def execute(
        self,
        action: AutomationAction,
        data: list,
        single_step: bool = False,
        plugin_dict: dict | None = None,
    ) -> tuple[str, dict | list]:
        """Evaluate the condition and route into the matching branch.

        :returns: ``(WAITING, {"condition": ...})`` while the chosen branch
            executes, ``(COMPLETED, data)`` for a missing/empty branch
            (pass-through), and ``(COMPLETED, branch_output)`` once the
            branch chain has finished.
        """
        # As for a split: a child that has been replayed is superseded by its
        # replacement and must not be counted, or a replayed branch fails its
        # parent forever.
        children = action.children.filter(replays__isnull=True)
        if not children.exists():
            condition_result = bool(evaluate_condition(self.condition, data))
            action._condition_result = condition_result
            branch = self._get_branch(condition_result)
            if branch is None or not branch.child_plugin_instances:
                # Missing or empty branch: nothing to do, pass data through.
                return COMPLETED, data
            return WAITING, {"condition": condition_result}
        if children.filter(state=FAILED).exists():
            return FAILED, {"error": "Conditional branch failed"}
        if children.filter(finished__isnull=True).exists():
            return WAITING, {}
        # Branch finished: complete with the branch end's output.
        branch = self._get_branch(self._branch_taken(action, children))
        output = data
        if branch and branch.child_plugin_instances:
            end_uuid = self._uuid_of(branch.child_plugin_instances[-1])
            end_action = children.filter(plugin_ptr=end_uuid).order_by("-created").first()
            if end_action is not None and end_action.result is not None:
                output = end_action.result
        return COMPLETED, output

    def _branch_taken(self, action: AutomationAction, children) -> bool:
        """Work out which branch ran, from the action this conditional spawned.

        The choice is recorded in ``result`` when the branch starts, but
        ``result`` is not the node's to keep: failure propagation overwrites it
        with ``{"failed_action_id": ...}`` when something below dies. A
        conditional whose branch failed and was then replayed would come back
        with the choice gone, read it as ``False``, join on the wrong branch and
        hand its parent stale data — which inside a loop means repeating an
        iteration's side effects.

        The first action it spawned is the first plugin of the branch it chose,
        and nothing overwrites that. Superseded actions are skipped, so a replay
        answers the same as the attempt it replaced.
        """
        first = children.order_by("created").first()
        if first is not None:
            for outcome in (True, False):
                branch = self._get_branch(outcome)
                if (
                    branch
                    and branch.child_plugin_instances
                    and self._uuid_of(branch.child_plugin_instances[0]) == first.plugin_ptr
                ):
                    return outcome
        # No children to read: fall back to what was recorded when it started.
        recorded = (action.result or {}).get("condition") if isinstance(action.result, dict) else None
        return bool(recorded)

    def get_next_actions(self, action: AutomationAction) -> list[AutomationAction]:
        """Create the branch's first action while waiting; else continue flow."""
        if action.state == WAITING and not action.children.exists():
            condition_result = getattr(action, "_condition_result", None)
            if condition_result is None and isinstance(action.result, dict):
                condition_result = action.result.get("condition")
            branch = self._get_branch(bool(condition_result))
            if branch and branch.child_plugin_instances:
                first_plugin = branch.child_plugin_instances[0]
                if not hasattr(first_plugin, "uuid"):
                    first_plugin, _unused = first_plugin.get_plugin_instance()
                return [
                    AutomationAction.objects.create(
                        previous=action,
                        parent=action,
                        automation_instance=action.automation_instance,
                        plugin_ptr=first_plugin.uuid,
                        finished=None,
                    )
                ]
            return []
        return super().get_next_actions(action)


class LoopPluginModel(AutomationPluginModel):
    """A while loop: run the body for as long as a condition holds.

    The condition is evaluated *before* each iteration, so a condition that is
    already false runs the body zero times. Each iteration's output becomes the
    data the next condition is evaluated against and the next iteration
    receives, which is what lets a loop make progress towards its own exit.

    Mechanically this is the re-entrant ``WAITING`` node the engine already uses
    for splits: spawn the body, suspend, and be woken when it finishes. The
    difference is the termination rule — a split fans out once and joins, a loop
    goes round again — and that difference is why the engine counts re-entries
    separately from attempts. Without that, a loop of fifty iterations would look
    like fifty failed attempts and exhaust a retry budget it never touched.
    """

    #: Iterations after which the loop gives up. A while loop is the first
    #: construct here that can run forever, so the bound is not optional; it is
    #: an error rather than a quiet stop, because silently truncating a loop
    #: produces a wrong result that looks like a right one.
    DEFAULT_MAX_ITERATIONS = 100

    question = models.CharField(
        max_length=255,
        verbose_name=_("Description"),
        blank=True,
        help_text=_("What this loop repeats, e.g. 'While there are unprocessed rows'. Shown in the editor."),
    )
    condition = models.JSONField(
        verbose_name=_("Condition"),
        help_text=_(
            "Evaluated before every iteration. The loop runs while it is true. "
            "Use double curly braces {{ }} for data attribute resolution, e.g. {{ remaining }}."
        ),
        default=dict,
    )
    max_iterations = models.PositiveIntegerField(
        default=DEFAULT_MAX_ITERATIONS,
        verbose_name=_("Maximum iterations"),
        help_text=_("The loop fails once it exceeds this many iterations, rather than running forever."),
    )

    no_body = _("This loop repeats nothing. Add the plugins to repeat inside it in the structure board.")

    class Meta:
        verbose_name = _("Loop Plugin")
        verbose_name_plural = _("Loop Plugins")

    def messages(self) -> list[str]:
        """Get validation messages for this loop."""
        return [] if self._body() else [self.no_body]

    def _body(self) -> list:
        """The plugins this loop repeats: its own children, in order."""
        return list(self.child_plugin_instances or [])

    def _body_start_uuid(self):
        """Get the uuid of the first plugin in the body, which each iteration spawns."""
        body = self._body()
        return self._uuid_of(body[0]) if body else None

    def _body_end_uuid(self):
        """Get the uuid of the last plugin in the body, whose output carries forward."""
        body = self._body()
        return self._uuid_of(body[-1]) if body else None

    def _carried(self, action: AutomationAction, rows: list) -> list:
        """The data flowing into the next iteration: the previous one's output.

        On the first pass there is no previous iteration, so it is the loop's
        own input.
        """
        from .engine import normalize_rows

        if not self._iteration(action):
            return rows
        end_uuid = self._body_end_uuid()
        if end_uuid is None:
            return rows
        end_action = (
            action.children.filter(replays__isnull=True, plugin_ptr=end_uuid, finished__isnull=False)
            .order_by("-created")
            .first()
        )
        if end_action is None or end_action.result is None:
            return rows
        return normalize_rows(end_action.result)

    def get_next_payload(self, action: AutomationAction, state: str, output, rows: list):
        """Hand each iteration the previous iteration's output.

        The engine's default passes a fan-out node's own input to every
        successor, which is right for a split — its branches all start from the
        same data — and wrong for a loop, whose whole purpose is to change the
        data until the condition stops holding.
        """
        if state == COMPLETED:
            return output
        return self._carried(action, rows)

    def _iteration(self, action: AutomationAction) -> int:
        """How many iterations this loop has started, counted from its children.

        Derived rather than stored. The obvious place to keep a counter is the
        action's ``result``, as the conditional keeps its branch choice — but
        ``result`` does not belong to the node alone: failure propagation
        overwrites it with ``{"failed_action_id": ...}`` when a branch dies. A
        loop whose body failed and was then replayed would come back with its
        counter gone, mistake itself for a first pass, discard the replayed
        step's output and run the body again — duplicating whatever side effects
        it has, and starting ``max_iterations`` over.

        Every iteration spawns exactly one action for the first plugin of the
        body, so counting those is the same number and cannot be clobbered.
        Superseded actions are excluded, so a replay replaces an iteration
        rather than adding one.
        """
        start_uuid = self._body_start_uuid()
        if start_uuid is None:
            return 0
        return action.children.filter(replays__isnull=True, plugin_ptr=start_uuid).count()

    def execute(
        self,
        action: AutomationAction,
        data: list,
        single_step: bool = False,
        plugin_dict: dict | None = None,
    ) -> tuple[str, dict | list]:
        """Evaluate the condition and either start another iteration or finish.

        :returns: ``(WAITING, {"iteration": n})`` while the body runs,
            ``(COMPLETED, output)`` once the condition is false, and
            ``(FAILED, ...)`` if the body failed or the bound was exceeded.
        """

        # A child that has been replayed is superseded by its replacement, as
        # for splits and conditionals: counting it would fail the loop forever.
        children = action.children.filter(replays__isnull=True)

        if children.filter(state=FAILED).exists():
            return FAILED, {"error": "Loop body failed"}
        if children.filter(finished__isnull=True).exists():
            # The current iteration is still running.
            return WAITING, {"iteration": self._iteration(action)}

        if not self._body():
            # Nothing to repeat. Pass the data through rather than spin.
            return COMPLETED, data

        # The data the condition sees is the previous iteration's output, so a
        # loop can work towards its own exit. On the first pass it is the input.
        iteration = self._iteration(action)
        current = self._carried(action, data)

        if not bool(evaluate_condition(self.condition, current)):
            return COMPLETED, current

        if iteration >= self.max_iterations:
            return FAILED, {
                "error": (
                    f"Loop exceeded {self.max_iterations} iterations. Its condition never became false; "
                    f"check that the body changes the data the condition tests."
                ),
                "iterations": iteration,
            }

        return WAITING, {"iteration": iteration + 1}

    def get_next_actions(self, action: AutomationAction) -> list[AutomationAction]:
        """Start the next iteration, or continue past the loop once it is done.

        Unlike a split, which fans out once and gates on ``not children.exists()``,
        a loop spawns on every iteration. The gate is instead "nothing is still
        running": if the current iteration is unfinished there is nothing to do,
        and the child that finishes will wake this node again.
        """
        if action.state == WAITING:
            children = action.children.filter(replays__isnull=True)
            if children.filter(finished__isnull=True).exists():
                return []
            body = self._body()
            if not body:
                return []
            return [
                AutomationAction.objects.create(
                    previous=action,
                    parent=action,
                    automation_instance=action.automation_instance,
                    plugin_ptr=self._uuid_of(body[0]),
                    finished=None,
                )
            ]
        return super().get_next_actions(action)


class SplitPluginModel(AutomationPluginModel):
    """Plugin model for parallel execution of multiple workflow paths."""

    class Meta:
        verbose_name = _("Split Plugin")
        verbose_name_plural = _("Split Plugins")

    no_paths = _(
        "No paths have been added to this split. Each split needs at least one path to continue the automation flow. "
        "Please add at least one path plugin to this split plugin in the structure board."
    )

    def messages(self) -> list[str]:
        """Get validation messages for this split.

        :returns: List of warning messages if no paths are defined.
        :rtype: list[str]
        """
        if not self.child_plugin_instances or len(self.child_plugin_instances) == 0:
            return [self.no_paths]
        return []

    def _paths(self) -> list:
        """Get the non-empty AutomationPath children of this split."""
        return [
            child
            for child in (self.child_plugin_instances or [])
            if child.plugin_type == "AutomationPath" and child.child_plugin_instances
        ]

    def _branch_end_uuids(self) -> list:
        """Get the plugin uuids of the last plugin in each path."""
        uuids = []
        for path in self._paths():
            end_plugin = path.child_plugin_instances[-1]
            if not hasattr(end_plugin, "uuid"):
                end_plugin, _unused = end_plugin.get_plugin_instance()
            uuids.append(end_plugin.uuid)
        return uuids

    def get_next_actions(self, action: AutomationAction) -> list[AutomationAction]:
        """Create parallel actions for each path in the split.

        :param action: The current split action.
        :type action: AutomationAction
        :returns: List of actions for parallel path execution.
        :rtype: list[AutomationAction]
        """
        if action.state == WAITING and not action.children.exists():
            next_actions = []
            for path in self._paths():
                # Downcast to get the actual plugin instance with uuid
                child_instance, _unused = path.child_plugin_instances[0].get_plugin_instance()
                next_actions.append(
                    AutomationAction.objects.create(
                        previous=action,
                        parent=action,
                        automation_instance=action.automation_instance,
                        plugin_ptr=child_instance.uuid,
                        finished=None,
                    )
                )
            return next_actions
        return super().get_next_actions(action)

    def execute(
        self,
        action: AutomationAction,
        data: list,
        single_step: bool = False,
        plugin_dict: dict[CMSPlugin] | None = None,
    ) -> tuple[str, dict | list]:
        """Execute the split: fan out, then join once all paths finished.

        First execution returns ``WAITING`` (the engine then creates one
        action per path via :meth:`get_next_actions`). When a branch chain
        ends, the engine wakes this action again: any failed branch fails
        the split; once all branches finished, the split completes with the
        concatenated output rows of all branch ends (the join point).

        :returns: Tuple of (state, output).
        """
        from .engine import normalize_rows

        # A child that has been replayed is superseded: its replacement is a
        # sibling in the same set, and the original is kept only as history.
        # Counting it would make a replayed branch fail its join forever, and
        # would merge a dead branch's output alongside its replacement's.
        children = action.children.filter(replays__isnull=True)
        if not children.exists():
            if not self._paths():
                # Nothing to fan out to: pass data through.
                return COMPLETED, data
            return WAITING, {}
        if children.filter(state=FAILED).exists():
            return FAILED, {"error": "One or more split branches failed"}
        if children.filter(finished__isnull=True).exists():
            # A branch is still running; keep waiting.
            return WAITING, {}
        # Join: merge the outputs of all branch end actions.
        action.message = "Joined"
        merged: list = []
        end_actions = children.filter(plugin_ptr__in=self._branch_end_uuids())
        for end_action in end_actions:
            merged.extend(normalize_rows(end_action.result))
        return COMPLETED, merged


class BaseActionPluginModel(ToolMixin, AutomationPluginModel):
    """Base model for action plugins that perform tasks.

    Concrete action behavior is implemented in proxy subclasses (see
    ``djangocms_automation.actions``) which override :meth:`perform`.
    Configuration entered through the plugin's ``data_form`` is persisted
    in :attr:`config` as a mapping of field name to expression/template.

    Every action is also a *tool*: placed inside an AI step it can be offered
    to a model, with the fields below saying what the model may fill and what
    stays bound to the editor's expressions. Outside an AI step none of that
    applies — see :mod:`djangocms_automation.tool_mixin`.
    """

    config = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Configuration"),
        help_text=_("Field values (expressions or templates) entered in the plugin form."),
    )

    # -- wiring, when this action is a tool --------------------------------
    #
    # On the base rather than on a separate model because every action is a
    # possible tool: one table, one migration, and a third-party action needs
    # no work to be usable by an agent.

    tool_name = models.SlugField(
        max_length=64,
        blank=True,
        verbose_name=_("Called"),
        help_text=_("What the model calls this. Defaults to the action's own name."),
    )
    tool_description = models.TextField(
        blank=True,
        verbose_name=_("When to use it"),
        help_text=_(
            "The only thing telling the model to reach for this rather than something else, and "
            "so the setting with the most influence on whether the step works. Say what it does "
            "and when to use it — not what it is."
        ),
    )
    requires_approval = models.BooleanField(
        default=None,
        null=True,
        blank=True,
        verbose_name=_("Requires approval"),
        help_text=_("Pause for a person to approve each call before it runs."),
    )
    exposed_fields = models.JSONField(
        default=list,
        blank=True,
        editable=False,
        verbose_name=_("Inputs the model may fill"),
        help_text=_("Written by the switches beside each input; not edited directly."),
    )

    #: Config keys holding a mapping whose *values* this action resolves as
    #: expressions. Empty for most actions. A caller supplying such an input as
    #: literal values — a model calling this action as a tool — wraps each one
    #: so the resolver returns it untouched.
    expression_mappings: frozenset = frozenset()

    def _template_fields(self) -> set[str]:
        """Get the config field names that hold templates (Textarea widgets).

        Fields declared with a ``Textarea`` widget in the plugin's
        ``data_form`` are rendered with ``safe_render`` (``{{ path }}``
        substitution); all other fields are resolved as expressions.
        """
        from cms.plugin_pool import plugin_pool
        from django import forms as django_forms

        try:
            plugin_cls = plugin_pool.get_plugin(self.plugin_type)
        except KeyError:
            return set()
        data_form = getattr(plugin_cls, "data_form", None)
        if not data_form:
            return set()
        return {
            name for name, field in data_form.base_fields.items() if isinstance(field.widget, django_forms.Textarea)
        }

    def resolve_inputs(self, row: dict | None, rows: list, overrides: dict | None = None) -> dict:
        """Resolve all configured inputs against a data row.

        Expression fields are resolved with
        :func:`~djangocms_automation.utilities.expressions.resolve_expression`;
        template fields (Textarea widgets in the ``data_form``) are rendered
        with :func:`~djangocms_automation.utilities.templates.safe_render`.
        The context is the given row with the full row list available as
        ``data``.

        :param row: The current data row (or None).
        :param rows: All data rows.
        :param overrides: Values to use as they are, instead of resolving the
            configured expression for those keys. An editor's inputs are
            expressions over the automation's data; a value supplied at call
            time — by a model calling this action as a tool — is already the
            literal, and running it through the resolver would read it as a
            data path.
        :returns: Mapping of config field name to resolved value.
        """
        from .utilities.expressions import resolve_expression
        from .utilities.templates import safe_render

        context = {**(row or {}), "data": rows}
        template_fields = self._template_fields()
        # An action's ``perform`` resolves its own inputs, so a caller supplying
        # values cannot pass them down through it. Setting them on the instance
        # is what lets any existing action be called as a tool without being
        # changed to know about tools.
        overrides = overrides or getattr(self, "_input_overrides", None) or {}
        resolved = {}
        for key, value in (self.config or {}).items():
            if key in overrides:
                resolved[key] = overrides[key]
            elif value is None or value == "":
                resolved[key] = None
            elif key in template_fields:
                resolved[key] = safe_render(str(value), context)
            else:
                resolved[key] = resolve_expression(str(value), context)
        # An override for something the editor never configured is still an
        # input the action should receive.
        for key, value in overrides.items():
            resolved.setdefault(key, value)
        return resolved

    def do_work(
        self,
        action: AutomationAction,
        data: list,
        single_step: bool = False,
        plugin_dict: dict | None = None,
    ) -> tuple[str, list]:
        """Run :meth:`perform` and complete with its output.

        Named ``do_work`` rather than ``execute`` because ``execute`` belongs to
        :class:`~djangocms_automation.tool_mixin.ToolMixin`, which wraps this in
        the phases of a tool call when a model asked for it and calls it
        directly when a person drew it into a flow.

        Exceptions propagate to the engine, which records the action (and
        instance) as failed; :class:`~djangocms_automation.engine.ActionPause`
        pauses the action instead.
        """
        return COMPLETED, self.perform(action, data or [])

    def perform(self, action: AutomationAction, rows: list) -> list:
        """Perform the action's side effect and return the output rows.

        The default implementation passes the data through unchanged.
        Concrete actions (see :mod:`djangocms_automation.actions`) override
        this.

        :param action: The automation action being executed.
        :param rows: The incoming data rows.
        :returns: The outgoing data rows.
        """
        return rows

"""The while loop: repeat a body for as long as a condition holds.

A loop is the first construct here that can run forever, and the first that
re-enters its own node many times in one run. Both are what these tests are
about: that it terminates, that it terminates for the right reason, and that
going round is never mistaken for failing.
"""

import pytest
from cms.api import add_plugin
from cms.models import Placeholder
from cms.plugin_base import CMSPluginBase
from cms.plugin_pool import plugin_pool
from django.contrib.contenttypes.models import ContentType

from djangocms_automation.instances import (
    COMPLETED,
    FAILED,
    AutomationAction,
    AutomationInstance,
)
from djangocms_automation.models import (
    Automation,
    AutomationContent,
    AutomationTrigger,
    BaseActionPluginModel,
    LoopPluginModel,
)


class CountdownModel(BaseActionPluginModel):
    """Decrements ``remaining`` by one, so a loop over it terminates."""

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        remaining = int(row.get("remaining", 0))
        return [{**row, "remaining": remaining - 1, "seen": int(row.get("seen", 0)) + 1}]


class LoopFailureModel(BaseActionPluginModel):
    """Fails terminally, to check a failing body fails the loop."""

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        raise ValueError("body blew up")


class NoProgressModel(BaseActionPluginModel):
    """Never changes the data, so the loop's condition can never become false."""

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        return rows


@plugin_pool.register_plugin
class CountdownPlugin(CMSPluginBase):
    model = CountdownModel
    name = "Countdown Plugin"
    render_template = "djangocms_automation/plugins/action.html"


#: Controls FlakyBodyModel: fail the next run, and count the successful ones.
BODY_RUNS: dict = {"ok": 0, "fail_next": False}


class FlakyBodyModel(BaseActionPluginModel):
    """Fails on demand, so a failed iteration can be replayed."""

    class Meta:
        proxy = True
        app_label = "djangocms_automation"

    def perform(self, action, rows):
        if BODY_RUNS.get("fail_next"):
            raise ValueError("body failed on purpose")
        BODY_RUNS["ok"] += 1
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        return [{**row, "remaining": int(row.get("remaining", 0)) - 1}]


@plugin_pool.register_plugin
class FlakyBodyPlugin(CMSPluginBase):
    model = FlakyBodyModel
    name = "Flaky Body Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@plugin_pool.register_plugin
class LoopFailurePlugin(CMSPluginBase):
    model = LoopFailureModel
    name = "Loop Failure Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@plugin_pool.register_plugin
class NoProgressPlugin(CMSPluginBase):
    model = NoProgressModel
    name = "No Progress Plugin"
    render_template = "djangocms_automation/plugins/action.html"


@pytest.fixture
def automation(db):
    return Automation.objects.create(name="Loop", is_active=True)


@pytest.fixture
def automation_content(automation, admin_user):
    return AutomationContent.objects.with_user(admin_user).create(automation=automation, description="Loop content")


@pytest.fixture
def run_setup(automation_content, settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}
    trigger = AutomationTrigger.objects.create(
        automation_content=automation_content, slot="start", type="click", position=0
    )
    placeholder = Placeholder.objects.get_or_create(
        content_type=ContentType.objects.get_for_model(AutomationContent),
        object_id=automation_content.pk,
        slot="start",
    )[0]
    return trigger, placeholder


def build_loop(placeholder, settings, condition, body_plugins=("CountdownPlugin",), max_iterations=100):
    """Add a loop whose body plugins are its own children, and return the loop."""
    loop = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationLoop",
        language=settings.LANGUAGE_CODE,
        condition=condition,
        max_iterations=max_iterations,
    )
    for plugin_type in body_plugins or ():
        add_plugin(placeholder=placeholder, plugin_type=plugin_type, language=settings.LANGUAGE_CODE, target=loop)
    return loop


#: "remaining > 0" in the ConditionBuilderWidget's schema.
GREATER_THAN_ZERO = {"logic": "and", "conditions": [{"field": "remaining", "operator": ">", "value": "0"}]}


def loop_action():
    return AutomationAction.objects.filter(parent__isnull=True).latest("id")


# --------------------------------------------------------------------------
# Terminating normally
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_loop_runs_its_body_until_the_condition_is_false(run_setup, settings):
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO)

    trigger.trigger_execution(data=[{"remaining": 3}])

    loop = loop_action()
    assert loop.state == COMPLETED
    assert loop.children.count() == 3, "the body ran once per iteration"
    assert AutomationInstance.objects.latest("id").status == COMPLETED


@pytest.mark.django_db
def test_the_loop_output_is_the_last_iterations_output(run_setup, settings):
    """Each iteration feeds the next, which is what lets the loop exit."""
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO)

    trigger.trigger_execution(data=[{"remaining": 3}])

    instance = AutomationInstance.objects.latest("id")
    assert instance.data[0]["remaining"] == 0
    assert instance.data[0]["seen"] == 3


@pytest.mark.django_db
def test_a_condition_that_is_already_false_runs_the_body_zero_times(run_setup, settings):
    """It is a *while* loop: the condition is checked before the body, not after."""
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO)

    trigger.trigger_execution(data=[{"remaining": 0}])

    loop = loop_action()
    assert loop.state == COMPLETED
    assert loop.children.count() == 0, "the body must not run at all"
    assert AutomationInstance.objects.latest("id").data == [{"remaining": 0}]


@pytest.mark.django_db
def test_the_flow_continues_after_the_loop(run_setup, settings):
    """A loop is a step in a chain, not the end of one."""
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO)
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE)

    trigger.trigger_execution(data=[{"remaining": 2}])

    top_level = AutomationAction.objects.filter(parent__isnull=True).order_by("id")
    assert top_level.count() == 2, "the plugin after the loop must have run"
    assert all(action.state == COMPLETED for action in top_level)


# --------------------------------------------------------------------------
# Going round is not failing
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_iterations_are_re_entries_not_attempts(run_setup, settings):
    """The reason a loop is safe to build on this engine.

    Every iteration re-claims the loop's own action. Counted as attempts, a
    loop of any length would exhaust its retry budget and be dead-lettered for
    working correctly.
    """
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO)

    trigger.trigger_execution(data=[{"remaining": 4}])

    loop = loop_action()
    assert loop.attempt_count == 1, "iterating is not retrying"
    assert loop.re_entry_count >= 4
    assert loop.dead_lettered is False


# --------------------------------------------------------------------------
# Termination guarantees
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_loop_that_never_progresses_fails_at_its_bound(run_setup, settings):
    """A wrong answer that looks right is worse than a loud failure."""
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO, body_plugins=("NoProgressPlugin",), max_iterations=5)

    trigger.trigger_execution(data=[{"remaining": 1}])

    loop = loop_action()
    assert loop.state == FAILED
    assert "exceeded 5 iterations" in loop.result["error"]
    assert loop.dead_lettered is True, "it must be inspectable afterwards"
    assert AutomationInstance.objects.latest("id").status == FAILED


@pytest.mark.django_db
def test_the_bound_is_configurable(run_setup, settings):
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO, body_plugins=("NoProgressPlugin",), max_iterations=2)

    trigger.trigger_execution(data=[{"remaining": 1}])

    loop = loop_action()
    assert loop.state == FAILED
    assert loop.children.count() == 2, "it stopped at the bound, not before or after"


@pytest.mark.django_db
def test_a_loop_with_no_body_passes_data_through(run_setup, settings):
    """An empty loop must not spin; it has nothing to run."""
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO, body_plugins=())

    trigger.trigger_execution(data=[{"remaining": 5}])

    loop = loop_action()
    assert loop.state == COMPLETED
    assert AutomationInstance.objects.latest("id").data == [{"remaining": 5}]


# --------------------------------------------------------------------------
# Failure inside the body
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_failing_body_fails_the_loop(run_setup, settings):
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO, body_plugins=("LoopFailurePlugin",))

    trigger.trigger_execution(data=[{"remaining": 3}])

    loop = loop_action()
    assert loop.state == FAILED
    assert AutomationInstance.objects.latest("id").status == FAILED


# --------------------------------------------------------------------------
# Editor validation
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_loop_with_nothing_in_it_warns_the_editor(run_setup, settings):
    _, placeholder = run_setup
    loop = build_loop(placeholder, settings, GREATER_THAN_ZERO, body_plugins=())
    instance = LoopPluginModel.objects.get(pk=loop.pk)
    instance.child_plugin_instances = []

    assert any("repeats nothing" in str(message) for message in instance.messages())


@pytest.mark.django_db
def test_a_loop_with_a_body_has_nothing_to_warn_about(run_setup, settings):
    _, placeholder = run_setup
    loop = build_loop(placeholder, settings, GREATER_THAN_ZERO)
    instance = LoopPluginModel.objects.get(pk=loop.pk)
    instance.child_plugin_instances = list(instance.cmsplugin_set.all())

    assert instance.messages() == []


@pytest.mark.django_db
def test_a_multi_step_body_runs_in_order_each_iteration(run_setup, settings):
    """The body is a sequence, not a single plugin: every step runs each time."""
    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO, body_plugins=("CountdownPlugin", "ActionPlugin"))

    trigger.trigger_execution(data=[{"remaining": 2}])

    loop = loop_action()
    assert loop.state == COMPLETED
    # Two iterations of a two-step body. Every step in a body is parented to the
    # loop, exactly as every step in a split's path is parented to the split, so
    # "is this iteration still running" stays a single query over the children.
    assert loop.children.count() == 4
    assert AutomationAction.objects.filter(automation_instance=loop.automation_instance).count() == 5


# --------------------------------------------------------------------------
# Replaying a failed iteration
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_replaying_a_failed_iteration_does_not_repeat_it(run_setup, settings):
    """A replayed iteration replaces one; it must not start a fresh loop.

    The loop's iteration count cannot live in its ``result``: failure
    propagation overwrites that with ``{"failed_action_id": ...}`` when the body
    dies. A loop coming back from a replay would find no count, take itself for
    a first pass, throw away the replayed step's output and run the body again —
    duplicating its side effects and starting ``max_iterations`` over.
    """
    from djangocms_automation import engine

    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO, body_plugins=("FlakyBodyPlugin",), max_iterations=1)

    BODY_RUNS["ok"] = 0
    BODY_RUNS["fail_next"] = True
    trigger.trigger_execution(data=[{"remaining": 1}])

    loop = loop_action()
    failed = loop.children.filter(state=FAILED).first()
    assert failed is not None, "the body failed as the test intended"
    assert BODY_RUNS["ok"] == 0

    # The loop's own counter has been overwritten by failure propagation.
    loop.refresh_from_db()
    assert "iteration" not in (loop.result or {})

    BODY_RUNS["fail_next"] = False
    engine.replay_action(failed.pk)

    loop.refresh_from_db()
    assert BODY_RUNS["ok"] == 1, "the body ran once more than once"
    assert loop.state == COMPLETED
    assert loop.children.filter(replays__isnull=True, state=COMPLETED).count() == 1


@pytest.mark.django_db
def test_a_replayed_iteration_does_not_reset_the_bound(run_setup, settings):
    """max_iterations counts iterations, and a replay replaces one."""
    from djangocms_automation import engine

    trigger, placeholder = run_setup
    build_loop(placeholder, settings, GREATER_THAN_ZERO, body_plugins=("FlakyBodyPlugin",), max_iterations=1)

    BODY_RUNS["ok"] = 0
    BODY_RUNS["fail_next"] = True
    trigger.trigger_execution(data=[{"remaining": 5}])  # would loop forever unbounded

    loop = loop_action()
    failed = loop.children.filter(state=FAILED).first()
    BODY_RUNS["fail_next"] = False
    engine.replay_action(failed.pk)

    loop.refresh_from_db()
    # One iteration is all the bound allows, replay included.
    assert BODY_RUNS["ok"] == 1
    assert loop.state == FAILED
    assert "exceeded 1 iterations" in loop.result["error"]


@pytest.mark.django_db
def test_replaying_a_failed_branch_inside_a_loop_keeps_the_iteration(run_setup, settings):
    """A conditional inside a loop must survive a replay of its branch.

    The conditional records which branch it chose in its ``result``, and failure
    propagation overwrites that when the branch dies. Read back after a replay
    it looks like ``False``, so the conditional joins on the wrong branch and
    hands the loop stale data — and the loop, seeing its input unchanged,
    repeats the iteration and hits its bound.
    """
    from djangocms_automation import engine

    trigger, placeholder = run_setup
    loop = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationLoop",
        language=settings.LANGUAGE_CODE,
        condition=GREATER_THAN_ZERO,
        max_iterations=1,
    )
    conditional = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationIf",
        language=settings.LANGUAGE_CODE,
        target=loop,
        condition=GREATER_THAN_ZERO,
    )
    yes = add_plugin(
        placeholder=placeholder, plugin_type="ThenPlugin", language=settings.LANGUAGE_CODE, target=conditional
    )
    add_plugin(placeholder=placeholder, plugin_type="FlakyBodyPlugin", language=settings.LANGUAGE_CODE, target=yes)
    add_plugin(placeholder=placeholder, plugin_type="ElsePlugin", language=settings.LANGUAGE_CODE, target=conditional)

    BODY_RUNS["ok"] = 0
    BODY_RUNS["fail_next"] = True
    trigger.trigger_execution(data=[{"remaining": 1}])

    # The leaf failure: the step inside the branch, not the conditional above it.
    # Replaying the conditional would sidestep the bug, because a fresh
    # conditional re-evaluates its condition instead of recalling its choice.
    failed = AutomationAction.objects.filter(state=FAILED, children__isnull=True).order_by("id").first()
    assert failed is not None, "the branch step failed as the test intended"
    assert failed.parent is not None and failed.parent.parent_id is not None, "it sits under the conditional"

    BODY_RUNS["fail_next"] = False
    engine.replay_action(failed.pk)

    loop_row = loop_action()
    assert BODY_RUNS["ok"] == 1, "the branch ran more than once"
    assert loop_row.state == COMPLETED, f"the loop did not finish: {loop_row.result}"
    assert AutomationInstance.objects.latest("id").data[0]["remaining"] == 0


# --------------------------------------------------------------------------
# Editor rendering
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_loop_is_marked_empty_only_when_it_has_no_children(run_setup, settings):
    """The ``empty`` class is what the editor greys the node with.

    A loop's children *are* its body, unlike a conditional's, whose children are
    branch containers that must themselves hold something. Reusing the
    conditional's test here marked every loop empty, however full.
    """
    from djangocms_automation.cms_plugins import AutomationLoop

    _trigger, placeholder = run_setup
    loop = build_loop(placeholder, settings, GREATER_THAN_ZERO)
    plugin = AutomationLoop()

    filled = LoopPluginModel.objects.get(pk=loop.pk)
    filled.child_plugin_instances = list(filled.cmsplugin_set.all())
    assert plugin.render({}, filled, None)["empty"] is False

    emptied = LoopPluginModel.objects.get(pk=loop.pk)
    emptied.child_plugin_instances = []
    assert plugin.render({}, emptied, None)["empty"] is True


@pytest.mark.django_db
def test_a_body_of_only_leaf_plugins_is_not_empty(run_setup, settings):
    """The specific shape the conditional's test got wrong.

    An action with no modifiers has no children of its own, so "do any of my
    children have children?" answers no for a perfectly full loop.
    """
    from djangocms_automation.cms_plugins import AutomationLoop

    _trigger, placeholder = run_setup
    loop = build_loop(placeholder, settings, GREATER_THAN_ZERO, body_plugins=("CountdownPlugin",))
    instance = LoopPluginModel.objects.get(pk=loop.pk)
    instance.child_plugin_instances = list(instance.cmsplugin_set.all())

    assert all(not child.cmsplugin_set.exists() for child in instance.child_plugin_instances)
    assert AutomationLoop().render({}, instance, None)["empty"] is False


# --------------------------------------------------------------------------
# Replaying the other branch
# --------------------------------------------------------------------------


#: "flag > 0" — used to steer a conditional down its Else branch.
FLAG_SET = {"logic": "and", "conditions": [{"field": "flag", "operator": ">", "value": "0"}]}


@pytest.mark.django_db
def test_replaying_a_failed_else_branch_inside_a_loop(run_setup, settings):
    """The same replay, down the branch the previous test does not take.

    Deriving the branch from the spawned action has to answer ``False`` as
    deliberately as it answers ``True``. Falling back to a default would look
    correct here by accident, so this pins the Else side against a later change
    to that default or an inverted derivation.
    """
    from djangocms_automation import engine

    trigger, placeholder = run_setup
    loop = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationLoop",
        language=settings.LANGUAGE_CODE,
        condition=GREATER_THAN_ZERO,
        max_iterations=1,
    )
    conditional = add_plugin(
        placeholder=placeholder,
        plugin_type="AutomationIf",
        language=settings.LANGUAGE_CODE,
        target=loop,
        condition=FLAG_SET,
    )
    yes = add_plugin(
        placeholder=placeholder, plugin_type="ThenPlugin", language=settings.LANGUAGE_CODE, target=conditional
    )
    add_plugin(placeholder=placeholder, plugin_type="ActionPlugin", language=settings.LANGUAGE_CODE, target=yes)
    no = add_plugin(
        placeholder=placeholder, plugin_type="ElsePlugin", language=settings.LANGUAGE_CODE, target=conditional
    )
    add_plugin(placeholder=placeholder, plugin_type="FlakyBodyPlugin", language=settings.LANGUAGE_CODE, target=no)

    BODY_RUNS["ok"] = 0
    BODY_RUNS["fail_next"] = True
    # flag is 0, so the conditional takes Else, where the failing step sits.
    trigger.trigger_execution(data=[{"remaining": 1, "flag": 0}])

    failed = AutomationAction.objects.filter(state=FAILED, children__isnull=True).order_by("id").first()
    assert failed is not None, "the Else branch step failed as the test intended"

    BODY_RUNS["fail_next"] = False
    engine.replay_action(failed.pk)

    loop_row = loop_action()
    assert BODY_RUNS["ok"] == 1, "the Else step ran more than once"
    assert loop_row.state == COMPLETED, f"the loop did not finish: {loop_row.result}"
    assert AutomationInstance.objects.latest("id").data[0]["remaining"] == 0

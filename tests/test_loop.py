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

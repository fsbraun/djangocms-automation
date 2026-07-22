Architecture Overview
=====================

This page describes the high-level architecture of `djangocms-automation`:
how automations are authored in the CMS editor, how building blocks (triggers,
nodes and modifiers) are structured, and how execution is driven by the task
framework at runtime.

Authoring Automations
---------------------

**Automations** are created and edited using the django CMS frontend editor and
its structure board. The automation is shown as a flow diagram in the preview
area of the frontend editor.

For every configured **trigger** (for example: click, form submission, timer)
there is a dedicated placeholder on the
:class:`~djangocms_automation.models.AutomationContent` instance.

Each position in the flow (a "node") is a CMS plugin. This lets editors
visually compose and nest complex flows directly in the page editor.
Nodes are modified by double clicking on their representation in the flow
diagram, or by clicking on the edit button in the structure board.

Action Nodes and Modifiers
--------------------------

A node corresponds to a plugin (e.g. action plugins or flow control
plugins). Nodes may have **modifiers** as child plugins that enhance or modify
the behaviour of the node. Modifiers can:

- add information to the automation,
- introduce pauses/delays, or
- provide access to external APIs (including LLM/AI integrations).

Control Flow and Nesting
------------------------

The nested plugin tree is used to build control flow constructs:

- Conditionals (If / Then / Else)
- Splits / Paths (parallel branches)
- Joins (re-joining branches)

This nested structure enables both linear sequences and complex branching
workflows.

Runtime Execution
-----------------

Automations are executed via Django's task framework (see
:doc:`../reference/tasks`). For deterministic test runs the immediate
backend can be used.

Each action execution is represented by an :class:`~djangocms_automation.instances.AutomationAction`
instance which is persisted. Persisted state enables monitoring, resuming and auditing
automation runs.

An action also carries execution-attempt metadata: ``attempt_count``,
``max_attempts``, ``started``, ``heartbeat_at``, ``next_attempt_at``,
``timeout_seconds`` and a per-attempt ``lease_id``. Claiming an action creates a
new lease and increments its attempt count. Normal claim, completion, failure,
pause and wait transitions create immutable
:class:`~djangocms_automation.instances.AutomationActionEvent` records. Failed
actions additionally store a machine-readable ``error_type`` and an
``error_detail`` summary alongside their existing result traceback.

The attempt fields are the foundation for durable retries and crash recovery;
automatic retry scheduling, heartbeat emission, expired-lease recovery and
timeout enforcement are not implemented yet.

Orchestration is owned by the execution engine
(``djangocms_automation.engine``):

- ``run_action`` atomically *claims* an action (``PENDING`` → ``RUNNING``),
  increments the attempt count, assigns a lease and records the transition.
  Double enqueues are no-ops. It then builds the linked plugin tree,
  dispatches ``plugin.execute()`` and handles the outcome.
- Fan-out nodes (Split, Conditional) go ``WAITING`` while their branch
  chains run; when a branch ends, the engine wakes the parent exactly once
  (an atomic ``WAITING`` → ``PENDING`` flip). A completed Split *joins* its
  branches by concatenating their end outputs.
- Failures are **fail-fast**: a failed action fails its waiting ancestors
  and marks the :class:`~djangocms_automation.instances.AutomationInstance`
  as ``FAILED`` with a ``finished`` timestamp. The failed action records its
  exception type, detail and transition event. No silent stops.
- Actions can pause themselves (``ActionPause``) until a given time —
  e.g. for rate-limit backoff — and are revived by the ``runautomations``
  management command.

Data Flow and Serialization
----------------------------

Automation data is serialized as JSON and passed between nodes through an in-memory
list. Each item in the list represents a row of data, akin to rows in a table. When
a node executes, it performs bulk operations on all data in the list; the results
are then passed to the next node in the pipeline.

For example, if a trigger collects user input, the data is converted to JSON and
wrapped in a list. As the automation proceeds through action nodes, each node
consumes the list, applies its logic (often in bulk to all rows), and produces
a new list as output for the next node.

This list-based approach enables efficient batch processing and allows modifiers
to enrich or transform the entire dataset before it is passed downstream.

Pausing and Reviving
--------------------

Actions may set a timestamp to pause execution (delay), either by raising
``djangocms_automation.engine.ActionPause`` from an action or via
``engine.pause_action``.

Paused automations are removed from the current execution pipeline and
revived by the ``runautomations`` management command, which should be run
periodically via cron. The command re-enqueues paused actions whose waiting
time has expired and fires due timer triggers.

Actions may also wait for **human input**: the *Wait for User* action sets
``requires_interaction`` and pauses the run until a permitted user resumes
it from the admin (Execution Instances → Open tasks).

Actions
-------

Concrete actions live in ``djangocms_automation.actions`` as proxy models of
:class:`~djangocms_automation.models.BaseActionPluginModel`, overriding
``perform(action, rows) -> rows``. Inputs are declared on the CMS plugin via
a ``data_form``; entered values (expressions or ``{{ path }}`` templates)
are persisted in the plugin's ``config`` JSON field and resolved against the
automation data at runtime. Shipped actions:

- **Send Email** — one email per data row via Django's email framework.
- **Create/Update/Query Records** — Django model CRUD, gated by the
  ``AUTOMATION_ALLOWED_MODELS`` setting (deny-all by default).
- **LLM Prompt** — provider-independent LLM completions via LiteLLM
  (``AUTOMATION_LLM_MODELS`` setting; API keys from the *Secrets* store).
- **Wait for User** — human-in-the-loop pause/resume.

See :doc:`../howto/actions` for configuration details.

Implementation notes
--------------------

The data model layer is implemented in ``djangocms_automation.models``; see
the API reference for models at :doc:`../reference/models` for detailed model
descriptions and field information. Runtime state lives in
``djangocms_automation.instances``, orchestration in
``djangocms_automation.engine``, and the task entry points in
``djangocms_automation.tasks``; see :doc:`../reference/instances` and
:doc:`../reference/tasks` for runtime behaviour and examples. Helper
utilities (expression resolution, ``{{ path }}`` templates, the condition
evaluator and JSON serialization helpers) are provided in
``djangocms_automation.utilities`` — see :doc:`../reference/utilities` for
usage notes and edge cases.

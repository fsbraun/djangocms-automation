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

Each action execution is represented by an :class:`~djangocms_automation.models.AutomationAction`
instance which is persisted. Persisted state enables monitoring, resuming and auditing
automation runs.

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

Actions may set a timestamp to pause execution (delay).

Paused automations are removed from the current execution pipeline
and revived by a management command that should be run
periodically via cron. This command checks for paused actions whose
waiting time has expired and re-enqueues them into the task queue.


Implementation notes
--------------------

The data model layer is implemented in ``djangocms_automation.models``; see
the API reference for models at :doc:`../reference/models` for detailed model
descriptions and field information. Execution and scheduling logic is located
in ``djangocms_automation.instances`` and ``djangocms_automation.tasks``; see
:doc:`../reference/instances` and :doc:`../reference/tasks` for runtime behaviour and
examples. Helper utilities (for example ``cleaned_data_to_json_serializable``)
are provided in ``djangocms_automation.utilities.json`` — see
:doc:`../reference/utilities` for usage notes and edge cases.


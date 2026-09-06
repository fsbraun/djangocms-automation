Glossary
========

These terms describe the single-item engine and editor. See
:doc:`explanation/reading-an-automation` for examples. Batch processing and
batch intake are deferred; those terms reserve wording for later work.

.. glossary::
   :sorted:

   Automation
      A process authored as a flow of steps in the CMS editor. Its triggers
      determine how a run starts.

   Run
      One execution of an automation, including its data, step executions,
      waiting states, and outcome. Called an execution instance in the admin.

   Step
      One position in the flow. A step can perform an action or control which
      steps run next. Developers also call it a node or plugin.

   Intent
      What a step is meant to achieve, expressed in its business name, such as
      "Notify the customer".

   Actor
      Who performs or is responsible for a step, such as a person, role, or
      automated system.

   Item
      One unit of automation data, such as a request or order, containing named
      fields. It retains its identity through ordinary
      steps, and its fields are isolated from those of other items.

   Field
      A named value within an item, such as Customer or Assessment. It can hold
      a simple value, a structured object, or a list. It is distinct from an
      action's configuration field and from a CMS placeholder slot.

   Batch
      A list of items processed together. Batch support is deferred from the
      first item-engine refactor. A batch is distinct from a list stored inside
      one item's field.

   List
      An ordered collection of values inside a field, such as an order's lines
      or accumulated review results. Its entries are not automatically
      independent automation items.

   Binding
      A saved selection connecting an action input to a source field or part of
      a field. The editor label is **Use data from**.

   Value template
      The common syntax for editable text inputs. Plain text is fixed text;
      ``{{ field.name }}`` reads item data; and a whole ``{{ 42 }}``,
      ``{{ true }}`` or ``{{ null }}`` keeps its native type. A reference mixed
      into surrounding text produces text.

   Output destination
      The field in which an action saves its result. The editor label
      is **Save result in**, with **Replace value** or **Append to list** as
      write choices. Replacing a value changes that field, not the whole item.

   Schema
      A description of data's structure, types, and required fields. A sample
      shows particular values; it is not a guarantee of every future value.

   Result structure
      The names and types an action returns, before they are saved into item
      fields. Fixed actions declare it; configurable actions such as Ask a
      Model let the editor define it with a schema.

   Automation fields
      A read-only catalogue at the trigger: starting fields and possible
      output fields in its path, with known types and sources but no values.
      Listing a field does not guarantee its presence during execution.

   Parallel paths
      Branches performing separate work on the same original item in private copies. The plugin type is Split.

   Join paths
      Reunite explicit writes from branches of the same original item.
      Distinct root fields are combined; writes to the same field conflict.
      This is different from collecting independent items.

   Repeat while
      Repeat steps while a condition holds, carrying updated fields into the
      next iteration, with a bounded number of iterations per instance.

   For each
      Repeat steps sequentially for captured entries in a list field within an
      item. It does not create independent items.

   Split into items
      Planned explicit operation to turn entries in a list into independent
      automation items. This changes item boundaries.

   Collect items
      Planned explicit operation to bring independent items together, for
      example to produce a total, group, or digest. This crosses item boundaries.

   Provenance
      Where a value came from: its originating item and the step execution,
      branch, or loop iteration that produced it.

   Execution occurrence
      One lease-owned execution of an action, identified in trace events by its
      lease UUID. Retries and continuations receive distinct occurrences.

   Execution trace
      Append-only evidence connecting a frozen definition to recorded inputs,
      decisions, writes, outcomes, and execution scopes. Retention deliberately
      removes payloads and leaves a visible redaction marker.

   Row
      A visual table row or database record, depending on context. The execution
      API uses a single item object, not a list of rows.

Glossary
========

These terms are used throughout the documentation. The item-preserving behavior
and editor labels described in :doc:`explanation/reading-an-automation` are the
agreed design; that page also explains the current implementation differences.

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
      fields. In the agreed design it retains its identity through ordinary
      steps, and its fields are isolated from those of other items.

   Field
      A named value within an item, such as Customer or Assessment. It can hold
      a simple value, a structured object, or a list. It is distinct from an
      action's configuration field and from a CMS placeholder slot.

   Batch
      A list of items processed together. It is distinct from a list stored
      inside one item's field. Processing items together does not, by itself,
      give them independent retry or failure handling.

   List
      An ordered collection of values inside a field, such as an order's lines
      or accumulated review results. Its entries are not automatically
      independent automation items.

   Binding
      A saved selection connecting an action input to a source field or part of
      a field. The planned editor label is **Use data from**.

   Output destination
      The field in which an action saves its result. The planned editor label
      is **Save result in**, with **Replace value** or **Append to list** as
      write choices. Replacing a value changes that field, not the whole item.

   Schema
      A description of data's structure, types, and required fields. A sample
      shows particular values; it is not a guarantee of every future value.

   Parallel paths
      Branches performing separate work on the same original item in the agreed
      design. The existing plugin type is Split.

   Join paths
      Reunite the work of branches of the same original item. The agreed design
      distinguishes this from collecting different items. The current split
      implementation instead concatenates its branch outputs.

   Repeat while
      Repeat steps while a condition holds, carrying updated fields into the
      next iteration. The agreed design gives each item its own loop state;
      the current Loop plugin carries the entire batch.

   For each
      Planned operation to repeat steps for entries in a list field within an
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

   Row
      The existing Python API term for an item, also useful when displaying
      items in a table. It does not mean a database record unless explicitly
      described as a database row. Existing identifiers such as ``rows`` remain
      unchanged.

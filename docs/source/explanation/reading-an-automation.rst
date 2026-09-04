Reading an automation
=====================

An automation is a process drawn as steps. Read each step's intent to understand
what it achieves, and its actor to see who does the work. For example, "Notify
the customer" explains the purpose of a Send Email action. Follow the paths to
see the order of work and where decisions or repeated steps occur.

Items, fields, and batches
-----------------------------

An :term:`item` is one unit of work, such as a customer request. Its named
:term:`fields <Field>` contain the data used by the process: Customer, Request,
Assessment, or Draft reply. Fields can contain simple values, structured
objects, or lists. Multiple items processed together form a :term:`batch`.

A list inside an item is different from a batch. One order with ten order lines
is one item with a list field. Ten independently processed orders are ten items.

.. note::

   The terminology is used throughout these guides. Preserving each item's
   fields, the field picker, and the operation labels below describe the agreed
   design and are not all implemented yet. See `Current behavior`_ when
   configuring an automation today.

The agreed data model
-------------------------

An automation processes items, individually or in batches. Each item carries
named fields. Steps read fields and save their results into fields, preserving
the rest of the item. Fields can hold single values, structured data, or lists.
Creating independent items or collecting items together requires an explicit
step.

For example, one request item could pass through these steps:

.. list-table::
   :header-rows: 1
   :widths: 30 40 30

   * - Step
     - Uses
     - Produces
   * - Find the customer
     - Request → Email
     - Customer
   * - Assess the request
     - Request, Customer
     - Assessment
   * - Draft a reply
     - Request, Assessment
     - Draft reply
   * - Notify the customer
     - Customer → Email, Draft reply → Body
     - Delivery status

Customer remains available after the assessment and drafting steps. Another
request item carries its own customer and assessment; ordinary steps do not
read or change fields belonging to other items in the batch. This data isolation
does not itself promise independent failure handling.

The planned action editor uses **Use data from** to choose a source, and **Save
result in** to select or create a destination field. **Replace value** changes
that field; **Append to list** adds a result to a list field. There is no need
to declare every intermediate field before drawing the process. The automation's
public output selects which fields to return.

Repeating and branching
-----------------------

**Repeat while** gives each item its own evolving state. The next iteration
receives the item's updated fields. Append results to a list field to retain
them across iterations. Different items can finish after different numbers of
iterations.

**For each** visits entries in a list field, such as order lines, within the
same item. **Split into items** instead makes those entries independent work
units. **Collect items** brings independent items together, for example to
calculate a total or send one digest.

**If** selects a path for an item. **Parallel paths** perform separate work on
the same original item; **Join paths** reunites that work. Joining paths is
different from collecting independent items. The proposed join preserves
unchanged fields and combines writes to distinct fields. Branches writing the
same field require an explicit resolution; conflict policies are still being
specified.

Current behavior
----------------

The engine currently passes a list of items between steps, called ``rows`` in
Python APIs. Output behavior depends on the action:

* Send Email, Create Record, and Update Records preserve incoming items and add
  result fields.
* Query Records replaces the current items with its query results. AI actions
  likewise produce new output items. There is no universal **Save result in**
  control yet.
* A split joins by concatenating its branch output lists. It does not yet
  reunite field changes by original item identity.
* The current Loop plugin carries the whole batch from one iteration to the
  next. Independent loop state for each item is planned.
* **For each**, **Split into items**, and **Collect items** are planned operations.

Use :doc:`../howto/actions` for the current action settings and expression
syntax. Existing expressions can also access the complete batch through
``data``; that legacy behavior is broader than the planned ordinary field
bindings.

Reading a run's data
-----------------------

An action execution records its incoming data as ``input_data`` and its outcome
as ``result``. These records provide raw item data for debugging, subject to
retention. On failure or while waiting, the result can contain diagnostic or
control information rather than successful output items. The recorded input
is the incoming batch, not necessarily the resolved parameters the action used.

The planned inspector adds four views:

* **Before**: the complete incoming items.
* **Used**: the resolved input values supplied to the action.
* **Changed**: the fields it added, replaced, or appended to.
* **After**: the resulting items.

Stable item identity and :term:`provenance` will connect those views across
branches, iterations, and replays. Current fields answer what the next step can
use; historical step data answers how those values evolved.

See the :doc:`../glossary` for the shared vocabulary, and
:doc:`execution-lifecycle` for the current execution and recovery behavior.

Reading an automation
=====================

Read each step's **intent** to understand what it achieves and its **actor** to
see who does the work. Follow the paths to see the order of work, decisions,
and repeated steps. **Uses** and **Produces** show the step's data connections.

One instance, one item
----------------------

Each automation instance processes one :term:`item`: a JSON object containing
named :term:`fields <Field>`. Fields hold simple values, structured objects,
or lists. One order with ten order lines is one item with a list field.
Ten independently processed orders require ten instances.

Actions read fields and save named results into fields, preserving everything
else. A value produced several steps earlier remains available by its field
name. There is no need to draw long data wires between distant steps.

.. list-table::
   :header-rows: 1

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

In the action editor, **Use data from** chooses a source. **Save result in**
selects or creates a destination field. **Replace value** changes that field;
**Append to list** adds one result to its list. An array result is appended as
one nested list, not flattened. Appending to a non-list fails.

Fields have stable machine keys (such as ``customer``) and optional display
labels. Changing a label does not change bindings. Nested expressions such as
``customer.email`` read part of a field; output writes target root fields.
A missing expression fails input resolution unless an explicit default is
configured. Fields produced only on another path may not be available.

An automation can select public output fields through ``output_fields``.
An empty selection returns the complete final item. The internal final item
remains available for debugging even when the public output is smaller.

Repeating and branching
-----------------------

**Repeat while** tests its condition before each iteration and carries the
updated item into the next one. **For each** captures a list when it starts,
then visits its entries sequentially. Use ``loop.entry`` for the current entry
and ``loop.index`` for its zero-based index. Changes to the source list do not
change the captured iteration sequence. Both loops have an iteration limit;
reaching it before finishing fails visibly. Nested loops restore the outer
loop's scope when the inner loop finishes.

To collect results, choose **Append to list** on an action inside the loop.
The list stays in the same item. Entries do not have independent instances,
retry policies, or concurrent execution.

**If** chooses a path. **Parallel paths** start from private copies of the same
item. **Join paths** preserves untouched fields and combines explicit writes
to distinct root fields. Two paths writing the same root field fail with a
conflict, even if they wrote equal values or both appended to a list. Use
separate destinations and combine their results in a following action.

Batch processing, batch intake, **Split into items**, and **Collect items**
are deferred. Top-level arrays are rejected; lists belong inside fields.
Several independent instances can still execute concurrently.

Reading a run's data
--------------------

Open an execution instance in the admin. Its **Executed definition** contains
the flow captured before execution was queued. Editing or deleting the CMS
plugins does not change that run, its resumed approvals, or its replay.

**Execution trace** contains chronological, occurrence-linked events:

* **Before**: the complete incoming item, recorded on the claim.
* **Used**: the resolved arguments supplied to the action.
* **Changed**: explicit named-result writes, including same-value writes.
* **After**: the accepted item after a successful execution.

The trace also records condition decisions, loop scopes, joins, attempts,
continuations, submitted human responses, and AI/tool conversation state.
Scheduling state is mutable; these historical events are separate and
append-only. A full visual debugger is future work; the admin exposes the
stored events as expandable JSON.

Outcome and successor scheduling commit together under the active lease.
A stale worker cannot commit another result, and duplicate task delivery does
not append a result twice. This does not guarantee exactly-once external
effects: a worker lost during an email or API request leaves the external
outcome **unknown**, which recovery records.

Retention deliberately redacts payloads, including frozen configuration,
loop entries, input/output snapshots, submissions, and AI conversations. The
redaction marker remains visible; replay is refused after required data is
removed. Access to the trace requires access to the execution-instance admin.

See :doc:`../howto/actions`, :doc:`execution-lifecycle`, and the
:doc:`../glossary` for details.

.. _automation-performance:

Performance: database coordination versus action work
=======================================================

Automation performance has two distinct parts. The engine performs database
work to make execution durable and race-safe; the action performs the useful
work requested by the automation. Whether an automation feels database-bound
or compute-bound depends mainly on the ratio between those two costs.

This distinction matters because optimizing the wrong side has little effect.
Removing one orchestration query will not materially speed up a ten-second API
call, while adding more workers will not make a chain of millisecond actions
cheap if every action still requires its own queue and state transitions.

The cost model
--------------

A useful approximation for one action attempt is:

.. code-block:: text

    worker time = queue dispatch
                + plugin-tree loading
                + action claim
                + action compute
                + outcome persistence
                + successor enqueue

Heartbeats run alongside ``action compute`` when the action lasts long enough.
Retries repeat most of the sequence. Branches perform it independently for each
child action.

The terms fall into three groups:

.. list-table:: Sources of execution cost
   :header-rows: 1
   :widths: 24 38 38

   * - Cost
     - What contributes to it
     - What it scales with
   * - Fixed orchestration
     - Queue claim and completion, action claim and outcome transitions,
       transition-event inserts, and successor enqueue
     - Number of action attempts and state transitions
   * - Workflow and payload
     - Loading the CMS plugin tree, creating branches, serializing JSON input
       and output, joins, heartbeats, and history retention
     - Automation size, branch count, payload size, and action duration
   * - Action work
     - Python computation, application-model queries and writes, email, HTTP,
       LLM, and other external calls
     - Row count and the action implementation

The engine intentionally does not promise an exact query count. The task
backend, CMS plugin types, branching structure, signals, retries, and database
backend all change it. The stable property is that each persisted action has a
non-zero coordination cost before and after its domain work.

Why the engine uses the database
--------------------------------

Database access is not incidental bookkeeping. It is the coordination
mechanism that provides durable delivery, exclusive claims, lease fencing,
auditable transitions, retries, joins, cancellation, and crash recovery.

For the durable database task backend, an ordinary action normally involves:

* inserting and later claiming a queued-task row;
* rebuilding the linked CMS plugin tree for the automation content;
* locking and updating the action when it is claimed, plus inserting its event;
* updating the action heartbeat if execution crosses a heartbeat interval;
* locking and updating the action outcome, plus inserting another event;
* creating successor actions and queued tasks; and
* marking the queued task successful.

Failure, replay, and branch joining add transitions of their own. Conversely,
the immediate and thread backends avoid the durable queue-table work, but they
also give up the survival and recovery properties required in production.

The important transaction boundary
----------------------------------

The engine holds database locks while it changes orchestration state, not while
the plugin computes. ``plugin.execute()`` runs outside a long transaction.
This keeps row-lock time short and allows heartbeats, cancellation, and lease
recovery to observe committed state while an action is running.

That boundary is both a correctness and performance choice. Wrapping a slow
HTTP request, LLM call, or large calculation in the claim transaction would
keep locks and database connections occupied for the entire action. With many
workers, lock wait and connection-pool pressure would then grow with action
duration rather than with the much shorter state transitions.

Short transactions do not mean that related writes may safely be separated.
State changes that must succeed together—such as recording an outcome and
making its continuation durable—need either a narrow transaction or explicit
reconciliation. Task enqueueing is deferred until commit. The design goal is
crash-safe orchestration without treating action compute as database work.

When action compute dominates
-----------------------------

For actions that call a remote API, generate content with an LLM, send many
messages, process a large batch, or perform substantial CPU work, the fixed
orchestration cost is usually small relative to ``perform()``. Performance is
then governed by the action itself:

* I/O-bound actions are limited by remote latency, rate limits, timeouts, and
  available worker concurrency.
* CPU-bound actions are limited by process capacity and the efficiency of the
  calculation.
* Database-bound actions are limited by their own query shape, locking, and
  transaction behavior—not primarily by the engine's state rows.

Adding workers helps independent I/O-bound actions until the remote service,
database connection pool, or task queue becomes the limiting resource. It does
not reduce the latency of one serial action chain.

When orchestration dominates
-----------------------------

The balance reverses for actions whose useful work takes only a few
milliseconds. A long linear chain of tiny transformations pays plugin-tree,
claim, event, outcome, and queue costs at every node. A wide split multiplies
that fixed cost by its number of branches. In this shape, more workers may
increase database contention without improving end-to-end latency.

The row-oriented action contract is the main amortization mechanism. One action
receives a list of data rows, so a plugin can transform or operate on a batch
under one orchestration envelope. Turning every row into a separate action
maximizes isolation and retry granularity, but also multiplies queue rows,
action rows, events, plugin-tree loads, and claims.

Combining cheap operations has the opposite trade-off. It reduces coordination
overhead, but produces a larger retry unit and less detailed per-step history.
Good action granularity follows the unit that should succeed, fail, and retry
together—not necessarily the smallest function that can be drawn as a node.

Payload size is database work
-----------------------------

Automation data is JSON stored on instances and actions. Input is persisted at
claim time so a failed action can be replayed with the data it actually saw;
results are stored for continuation and audit. Large row sets therefore cost
more than Python serialization alone: they increase database writes, storage,
replication traffic, backups, admin rendering, and later reads.

Batching many rows reduces per-row orchestration, but an unbounded batch can
replace query overhead with large JSON fields and high memory use. For large
documents or binary data, storing a reference to object storage is generally a
better execution payload than copying the content through every action result.

Plugin-tree loading
-------------------

Before an action is claimed, the engine rebuilds and downcasts the automation's
CMS plugin tree so it can resolve the action, its neighbors, and its branches.
This cost grows with the size of the automation, not just the current node. It
is often invisible beside a slow action but can become significant for large
automations made of tiny steps.

Normal execution deliberately does not keep a process-global tree cache. CMS
content can change, and safe cache invalidation would need to follow content
versioning and publication. Recovery does cache plugin maps within one recovery
batch because that cache has a short, explicit lifetime. Any broader caching
strategy trades database reads for staleness and invalidation complexity.

Heartbeat and scheduler load
----------------------------

A short action usually finishes before its heartbeat thread writes anything.
A long-running action refreshes one row periodically, at a fraction of
``AUTOMATION_LEASE_SECONDS``. Reducing the lease window detects dead workers
sooner, but increases write frequency for every concurrently running long
action. The lease should be considered against expected action duration,
scheduler cadence, and acceptable recovery delay rather than minimized in
isolation.

The scheduler also scans for expired leases, due retries, waiting joins, and
timers. More frequent ticks reduce wake-up latency but perform those scans more
often. The database-backed scheduler lock prevents several scheduler hosts from
doing the same work concurrently; it is an availability mechanism, not a way
to increase scheduler throughput.

Choosing the task backend
-------------------------

The database task backend favors operational simplicity and transactional
enqueueing. Its queue table is indexed and workers use locked claims, but the
database still coordinates both application state and task delivery. This is a
good fit for moderate automation volume where action work is meaningful
relative to dispatch.

At very high task rates, a broker-backed Django task backend can remove queue
polling and queue-row contention from the application database. It does not
remove action-state persistence, transition events, plugin-tree loading, or the
queries performed by plugins. Changing the backend therefore helps most when
queue dispatch is the measured bottleneck.

Reading measurements correctly
-------------------------------

End-to-end latency combines several clocks that answer different questions:

.. list-table:: Useful timing perspectives
   :header-rows: 1
   :widths: 34 66

   * - Measurement
     - What it reveals
   * - Trigger to queue start
     - Queue backlog and scheduler delay
   * - Queued-task start to finish
     - Total worker occupancy, including orchestration and action work
   * - Action ``started`` to ``finished``
     - The claimed attempt, dominated by plugin execution for expensive actions
   * - Database query time and lock wait
     - Coordination overhead and contention
   * - Remote-call or plugin-level timing
     - The useful action work independently of engine overhead

Percentiles are more informative than averages for automation workloads: a
small number of rate-limited calls, lock waits, retries, or oversized payloads
can dominate user-visible latency. Query counts should be paired with query
time; many indexed, local queries may be cheaper than one slow plugin query.

The central performance question is therefore not “How many queries does an
action use?” but “Is durable orchestration a meaningful fraction of this
action's total cost?” When it is, action granularity, batching, payload size,
tree size, and queue choice are the useful levers. When it is not, optimize or
scale the action's own computation and dependencies.

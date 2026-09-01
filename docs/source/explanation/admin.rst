Understanding the automation admin
==================================

The Django admin shows nine entries under **Automations**, and at first glance
they look like eight models to browse. They are not really that. They serve three
separate *concerns*, and most confusion about the admin comes from reading one as
another — treating a record of the past as something you can edit, or an
execution queue as a list you can tidy.

This page explains why the admin is divided the way it is, and what each
division is for. It does not describe the fields; the models themselves are
documented in :doc:`../reference/instances`.

Three concerns
--------------

**The definition** is what *should* happen: an automation, its triggers, and the
credentials it may use. You author this.

**The record** is what *did* happen: executions, the transitions each action went
through, and the failures that need attention. You read this.

**The machinery** is what is happening *right now*: queued work and the lock the
scheduler holds. You watch this, and only intervene when something is stuck.

The three are separated by tense, but also by audience: the person composing a
workflow, the person working out why a run went wrong, and the person keeping the
service alive are rarely the same person, and rarely need the same access.

The permissions mostly follow that split. Definition entries have full add and
change forms. Append-only event logs, dead letters, and queued tasks have no add
or editable change form; the scheduler lock also cannot be added and exposes
only read-only fields. **Execution Instances** is the exception among records:
Django exposes add and change forms for it, although manually creating or
editing an instance is not a supported execution workflow. That asymmetry is
deliberate, and the rest of this page is mostly about why.

Why the definition is barely in the admin
-----------------------------------------

The admin's **Automations** entry is thin: a name, an active flag, and a link to
the editor. That is because an automation's substance — the triggers, conditions,
branches and actions — is a django CMS plugin tree, authored in the frontend
editor where it can be seen as a diagram. Reproducing that as admin inlines would
mean maintaining a second, worse editor for the same data.

For the same reason **Triggers** is hidden from the admin index. A trigger only
means something in the context of one automation, so it is edited from the
workflow. The permissions still exist and are still checked — which is why
``change_automationtrigger`` appears when you assign permissions to a group even
though there is no link to click.

The one piece of definition that genuinely belongs in the admin is **Secrets**.
API keys are not part of a workflow's shape; they are environment, they rotate on
a different schedule, and they should be editable by people who never touch a
workflow. Keeping them separate also means a key can be deactivated without
touching any automation that uses it — which is the safe way to rotate, because
an automation running mid-rotation then fails with a clear configuration error
rather than a missing row.

Why the record cannot be edited
-------------------------------

Every state transition is written to an append-only log: which attempt, which
lease, what the engine was doing and why. Nothing in the admin can add to it or
edit it.

This is not caution for its own sake. The log's only value is as evidence. When a
run behaves strangely, the question is almost always "what actually happened, in
what order" — and an answer you could have edited is not an answer. The moment
the history is mutable, it stops being able to settle an argument about what the
system did. So the engine writes it and nothing else may.

It is read through the run rather than as a list of its own. Each action on an
instance has a **History** link showing the states it moved through, its
attempts, and what each transition recorded. A changelist over every event in
the database answered a question nobody asks — *every action that ever failed,
across every run* — while the question people actually have is about the run in
front of them.

A run's own transitions have no page at all. Started, finished, canceled: the
instance already carries its status and its timestamps, so a second place to
read them said nothing new, and a run still in flight had nothing to show there.
The events are still recorded, and remain the way a reopened run is told apart
from one that never finished.

**Execution Instances** is the same data seen from the other end: one row per run,
with its actions inline. It *does* have add and change forms, because it is an
ordinary model and Django generates them. Creating an execution by hand is not a
supported way to run an automation, though — triggers exist for that, and an
instance created by hand has no trigger, no initial data, and no reason to
believe the workflow it points at is the one you meant.

A distinction worth internalising
---------------------------------

Each action carries two counters that look interchangeable and are not.

``attempt_count`` counts executions **after a failure**. This is what a retry
budget spends: an action allowed three attempts may fail twice before it is
given up on.

``re_entry_count`` counts resumptions that are **not** failures. A split that
suspends while its branches run, then wakes to join them, has re-entered. A
paused action revived by the scheduler has re-entered. Neither has failed.

Keeping them apart matters more than it first appears. Before the distinction
existed, every resumption looked like another attempt, so a workflow that
legitimately suspended and resumed ten times would exhaust a retry budget it had
never actually touched. The counters are separate because *looping is not
failing*, and any node that suspends and resumes — a split today, a loop or an
agent later — depends on that being true.

Why failures appear in two places
---------------------------------

A failed action shows up inline under its execution *and* in **Dead letters**.
That looks like duplication. It is not: Dead letters is a filtered view of the
same rows, not a copy of them.

The alternative would have been a separate dead-letter table holding the failed
action's input, error, and attempt history. That was rejected because the action
already holds all of it. Copying it would create two versions of the same truth,
which then have to be kept in step, and which will eventually disagree. So Dead
letters is a proxy: same table, different question. Execution Instances asks
"what happened in this run"; Dead letters asks "what needs a human".

The consequence is worth knowing. Because it is the same row, deleting a dead
letter deletes the action, and the transition events cascade with it. Tidying the
dead-letter queue by deleting from it silently removes that run's history from
The event log too. This is the strongest argument for withholding the ``delete``
permission on these entries: the design intends them to be read-only, and only
Django's default permission set makes deletion available at all.

Why replay creates something new
--------------------------------

Replaying a dead letter does not reset the failed action and run it again. It
creates a *new* action, seeded with the input the failed attempt actually
received, linked back to the original through ``replayed_from``, and reopens the
instance.

Resetting the original would be simpler and is the obvious implementation. It was
rejected for the same reason the event log is immutable: it rewrites history. If
an action can be reset, then "this action failed twice and then succeeded" and
"this action succeeded" become indistinguishable afterwards, and the record loses
the thing you most want from it when something has gone wrong in production.

Storing the input on the action, rather than reading the instance's current data
at replay time, follows from the same concern. By the time you replay, the
instance's data may have moved on; replaying against it would not reproduce the
failure you are trying to fix.

One honest limitation: replay uses the currently published workflow, not the
version that was live when the run failed. If the workflow changed in between,
the replay follows the new definition. Pinning executions to an immutable
workflow version is a later piece of work, and until it lands this is a real edge
to keep in mind when replaying something old.

Why the machinery is visible at all
-----------------------------------

Most applications hide their queue. **Queued tasks** is
exposed because of how automations fail in practice.

When an automation misbehaves, it usually does something wrong — and the run
tells you what. But the most common production failure is that automations do
*nothing*, and that failure is invisible from the workflow: every execution looks
fine, simply frozen. There are only a few causes, and none of them are visible in
the workflow itself. Either no worker is draining the queue, or no scheduler is
firing timers and reviving retries, or a scheduler died holding the lock.

Making that visible turns an invisible problem into an obvious one. A growing
pile of ``READY`` tasks means work is arriving faster than it is being drained,
or nothing is draining it. It rarely requires action — a restarted worker drains
a backlog — but it answers "is the machinery alive?" in one glance.

**Scheduler locks** answers the other half of that question: a lock held long
past its expiry means a scheduler died mid-tick. It is not in the menu, because
it is one row that matters on the few days something is stuck, and a permanent
entry for it would be one more thing to scroll past on all the others. The page
is there for whoever goes looking.

The same reasoning explains why deleting a queued task is more dangerous than it
looks. A row in ``READY`` *is* the pending work: delete it and the action it
would have run stays ``PENDING`` with nothing left to enqueue it until the
scheduler's next revive pass. The queue is not a list of notifications to clear.

What the admin is not
---------------------

It is not where you build workflows — that is the frontend editor. It is not a
way to run them — that is what triggers are for. And it is not a monitoring
system: it will tell you that a queue is deep or a lock is stale, but it does not
alert, aggregate, or retain. The engine emits structured logs and signals for
that; see :doc:`../howto/deployment` for the signals worth alerting on.

What the admin is good at is answering questions about a *particular* run: what
happened, in what order, why it stopped, and what to do about it now.

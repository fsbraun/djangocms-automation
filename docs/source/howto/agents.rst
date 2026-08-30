Build an agent
==============

An **Ask a Model** step gives a model a task. On its own it answers and hands the
answer downstream — one call, one answer, a cost you know in advance. Put actions
inside it and it may use them, choosing for itself, until it has an answer.

That is the only difference. There is no separate agent plugin and no separate
kind of tool: an action inside an AI step *is* a tool.

Before you start
----------------

.. code-block:: bash

    pip install djangocms-automation[llm]

.. code-block:: python

    INSTALLED_APPS = [
        ...
        "djangocms_automation",
        "djangocms_automation.ai",
    ]

    AUTOMATION_LLM_MODELS = ["anthropic/claude-opus-4-8"]

Store the provider's API key under *Automations → Secrets*, with **Service** set
to the part of the model string before the slash — ``anthropic`` for the model
above.

Trying it without a provider
----------------------------

Building one means getting several things right at once — what the tools are,
which of their inputs the model may fill, what needs approving — and none of it
is visible until a run happens. There is a model string that answers locally so
you can get there first:

.. code-block:: python

    AUTOMATION_LLM_MODELS = ["dummy/echo"]

No key, no network, no ``litellm``. Pick ``dummy/echo`` as the step's model and
it replies with the task it was given, which is enough to check that the prompt
renders and the answer flows on. Directives in the **Task** make it do more:

.. code-block:: text

    Look this customer up and reply to them.

    !call find_customer {"filters": {"email": "ann@example.com"}}
    !call reply_to_customer {"subject": "Hello", "body": "Thanks for writing."}

Each ``!call`` is made in turn, with the result fed back exactly as a real model
would see it — so approval gates pause, a rejected argument comes back as an
error, and the transcript reads the way a real one does. ``!json {...}`` answers
with an object, for trying an **Output shape**; ``!fail some message`` makes the
provider call fail, for trying what the step does when it cannot answer.

It is gated like any other model: nothing uses it unless the project put it in
the allowlist and a step names it.

The shape of one
----------------

::

    Ask a Model  "Answer the customer"
    ├── Query Records   find_customer
    ├── Send Email      reply_to_customer
    └── Wait for User   escalate

Those three actions are the only things this step can do. There is no registry
to reach past: if the model asks for anything else, it is told what it actually
has and asked again.

Adding an action does not convert the step into something else. It gives it
something it may do, and the card says which it currently is.

Choosing what the model may fill
--------------------------------

This is the setting that matters most, and the one worth thinking about longest.
Inside an AI step, every input on an action's form grows a switch beside it:

.. code-block:: text

    Email Subject      ( ) expression   (o) the model decides
    Email Body         ( ) expression   (o) the model decides
    Recipient Email    (o) expression   ( ) the model decides    trigger.from
    Sender Email       (o) expression   ( ) the model decides    'support@example.com'

Flip an input to **the model decides** and it stops asking you for a value,
because it would never use one. Leave it on **expression** and it stays bound to
what you wrote — and the model is never shown that the input exists.

So a *Send Email* tool can let the model write the subject and body while the
recipient stays pinned to ``trigger.from``. Not by instruction: the field is
absent from what the model is given, and refused if it sends one anyway.

Start with the smallest set that lets the tool do its job.

Naming it for the model
-----------------------

Two fields in the same section, both defaulted from the action:

**Called** — what the model calls it. ``send_email`` unless you say otherwise.

**When to use it** — the only thing telling the model to reach for this rather
than something else, and so the setting with the most influence on whether the
step works. The default describes the mechanism, which is the wrong thing:

    *Send a reply to the customer who wrote in. Use this once you have an answer
    for them. Do not use it to ask a colleague something.*

Approval before anything irreversible
-------------------------------------

Every tool has an approval setting with three states, defaulting to
**Automatic**: an action whose effects cannot be taken back needs approval, and
everything else does not. Actions declare this themselves — *Send Email*,
*Create Record* and *Update Records* say so — so it follows what the action is
rather than what was true when you saved the form.

A call is checked before it is put to anyone. Arguments that could not run —
malformed JSON, a value the action's form rejects — come back to the model as a
correctable error instead of becoming somebody's task.

An approval pauses the call as a normal human-in-the-loop step: it appears under
*Automations → Execution Instances → Open tasks*, naming the tool, listing the
arguments the model chose, and saying so when the action cannot be undone. It
runs only once someone permitted resumes it — the call has not happened at that
point, and approving is what makes it happen.

An action can also wait for a person *of its own*: a *Wait for User* tool is how
a step escalates, and what the person answers is what the model hears back.

Limits
------

A step with tools decides its own next step, so nothing about its cost is
knowable in advance. Four limits bound a run, and they exist separately because
they fail differently:

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Limit
     - Catches
   * - Maximum turns
     - A model that will not conclude.
   * - Maximum tool calls
     - One that thrashes between tools without progressing. Applied before the
       calls run, so a turn asking for more than the run can afford has the
       excess refused rather than executed.
   * - Maximum tokens
     - A conversation growing faster than the work.
   * - Deadline
     - Everything else, including a provider that has become very slow.

Reaching any of them **fails** the run. That is deliberate: a step that stopped
quietly would return a confident partial answer with nothing marking it as
partial.

The same reasoning applies to a reply the *provider* cut short — at its token
limit, or through a content filter. Such a reply is fluent to its last word and
simply stops, so nothing downstream could tell it apart from an answer.

Shaping the answer
------------------

Without tools, an **Output shape** constrains the answer to a JSON schema: an
object becomes one data row, an array becomes rows. Downstream steps can then
read a field instead of parsing prose.

A step that has tools ignores it and says so in the editor. Constraining the
answer and offering tools on the same turn is provider-specific, and behaving
differently per provider is worse than declining.

Reading back what happened
--------------------------

Every tool call is its own execution step, so a run reads like any other
automation. Under *Execution Instances* you see the step and one child action
per tool call, each with the arguments it was given and what it returned — a
tool call *is* an action row, so there is nothing new to learn about reading it.
The conversation, turn count and token usage are kept on the step's action.

If a run fails, it appears in *Dead letters* like anything else and can be
replayed. A replayed call is the same call, and asks for approval again.

Writing a tool in Python
------------------------

There isn't a separate way to do it: write an action. See *Writing your own
action* in :doc:`actions`. An action works as a step in a flow and as a tool
from one piece of code, and a third-party action becomes a tool with no work by
anyone.

Two things worth setting on an action meant for agents:

.. code-block:: python

    class CancelSubscription(ActionPlugin):
        #: Sets the automatic approval gate.
        destructive = True
        #: Set False for an action that makes no sense unless a person chose it.
        can_be_tool = True

When not to use one
-------------------

If you can draw the path, draw it. A conditional and two actions are cheaper,
faster, and behave the same way every time. A step with tools earns its cost when
the path depends on something only a model can judge — what a message is about,
how to phrase a reply, which of several lookups is worth doing.

See also
--------

- :doc:`../explanation/tools-and-trust` — why the schema shown to the model is
  not what constrains it, and what actually does.
- :doc:`actions` — the actions a tool can be.

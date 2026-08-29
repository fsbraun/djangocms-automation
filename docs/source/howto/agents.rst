Build an agent
==============

An agent is a language model that chooses which of your actions to run, and
keeps going until it has an answer. Where an ordinary automation is a path you
draw, an agent is a goal you state and a set of tools you allow.

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

Store the provider's API key under *Automations → Secrets*, with **Service**
set to the part of the model string before the slash — ``anthropic`` for the
model above.

The shape of one
----------------

In the structure board, an agent contains tools, and each tool contains exactly
one action::

    Agent  "Answer the customer"
    ├── Tool  find_customer      → Query Records
    ├── Tool  reply_to_customer  → Send Email
    └── Tool  escalate           → Wait for User

Those three tools are the only things this agent can do. There is no registry to
reach past: if the model asks for anything else, it is told what it actually has
and asked again.

Choosing what the model may fill
--------------------------------

This is the setting that matters most, and the one worth thinking about
longest. Each tool has an **Inputs the model may fill** list, naming fields from
the action it wraps. Everything you leave out stays bound to the expression you
configured on the action, and is never shown to the model.

For a *Send Email* action, exposing ``subject`` and ``body`` and leaving
``recipient_email`` bound to ``trigger.from`` means the model writes the message
and cannot choose who receives it. Not by instruction — the field is absent from
what it is given, and refused if it sends one anyway.

Start with the smallest set that lets the tool do its job.

Writing the description
-----------------------

A tool's description is the only thing telling the model when to reach for it,
and it influences whether an agent works more than any other single setting.
Write what the tool does and when to use it, not what it is:

    *Send a reply to the customer who wrote in. Use this once you have an answer
    for them. Do not use it to ask a colleague something.*

Approval before anything irreversible
-------------------------------------

Any tool can be set to require approval. The call then pauses as a normal
human-in-the-loop step: it appears under *Automations → Execution Instances →
Open tasks*, showing the tool name and the arguments the model chose, and runs
only once someone permitted resumes it.

Tools wrapping actions whose effects cannot be taken back — creating or updating
records, sending mail — default to requiring approval when you add them. Turn it
off deliberately if you mean to; the default is set that way because the
opposite mistake cannot be undone.

Limits
------

An agent decides its own next step, so nothing about its cost is knowable in
advance. Four limits bound a run, and they exist separately because they fail
differently:

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Limit
     - Catches
   * - Maximum turns
     - A model that will not conclude.
   * - Maximum tool calls
     - One that thrashes between tools without progressing.
   * - Maximum tokens
     - A conversation growing faster than the work.
   * - Deadline
     - Everything else, including a provider that has become very slow.

Reaching any of them **fails** the run. That is deliberate: an agent that
stopped quietly would return a confident partial answer with nothing marking it
as partial.

Reading back what happened
--------------------------

Every tool call is its own execution step, so an agent run reads like any other
automation. Under *Execution Instances* you see the agent and one child action
per tool call, each with the arguments it was given and what it returned. The
conversation, turn count and token usage are kept on the agent's action.

If a run fails, it appears in *Dead letters* like anything else and can be
replayed.

When not to use one
-------------------

If you can draw the path, draw it. A conditional and two actions are cheaper,
faster, and behave the same way every time. An agent earns its cost when the
path depends on something only a model can judge — what a message is about, how
to phrase a reply, which of several lookups is worth doing.

See also
--------

- :doc:`../explanation/tools-and-trust` — why the schema shown to the model is
  not what constrains it, and what actually does.
- :doc:`actions` — the actions a tool can wrap.

Why a tool schema is not a boundary
===================================

When an action is offered to a model as a tool, two artefacts come out of the
same declaration: a JSON Schema sent to the provider, and a validation step that
runs when the model calls back. They look redundant. They are not, and treating
them as interchangeable is the mistake this page exists to prevent.

The schema is what the model is **told**. The validation is what the model is
**held to**.

Two sides of one thing
----------------------

An action and a tool are the same object seen from different directions.

An action's inputs are filled in by an editor, before the automation runs, as
expressions over the automation's data: ``customer.email``, ``{{ order.total }}``.
A tool's inputs are filled in by a model, while it runs, as literal values:
``"ada@example.com"``, ``42``. What the thing *does* — send the mail, write the
record — is identical either way.

That is why a tool here is not a new kind of object. It is an existing action
plus a statement of which of its inputs the model may supply. Everything else
is derived: the action's ``data_form`` already declares each input's type, its
label, its help text, whether it is required, and which values are permitted.
That is a schema in all but name, so the schema is generated from it rather than
written a second time. An action author writes nothing extra, and a tool cannot
describe an action that no longer exists.

Why the schema cannot be the boundary
-------------------------------------

It is tempting to read a schema with ``"additionalProperties": false`` as a
guarantee. It is not one, for a chain of reasons that each hold on their own.

A language model emits tokens. Nothing in it is obliged to respect a schema; a
schema is a very strong hint, not a type system. Providers differ in how firmly
they constrain output, versions change, and "strict" modes are best-effort
across the field. Then there is the part that has nothing to do with
correctness: the arguments a model produces are influenced by the text it has
read, and some of that text arrives from outside — an email body, a web page, a
customer's form submission. A model can be argued into asking for something its
schema never offered.

None of this makes schemas useless. Telling a model the permitted values of a
field is the cheapest way to stop it inventing one, and a good schema
dramatically reduces how often anything is rejected at all. It is simply the
wrong thing to *rely* on. It reduces mistakes; it does not prevent them.

So the guarantee lives on this side. Arguments are run back through the
action's own Django form, and any key the tool did not offer is refused —
separately from, and regardless of, the schema having omitted it. The form
coerces as well as rejects: a model that sends ``"3"`` for an integer gets ``3``,
because that is what the action expects.

Exposed and bound
-----------------

Because inputs are per-field, a tool does not have to offer all of them. Each
input is either *exposed* — the model may fill it — or *bound* by the editor to
an expression the model never sees.

This is the useful part. A *Send Email* tool can bind the recipient to
``trigger.from`` and expose only the subject and body: the model writes the
message, and cannot choose who receives it. A *Query Records* tool can bind the
model and expose only the search terms and the row limit. The blast radius of a
tool call is whatever the person who added the tool decided to open, and no
wider — enforced on this side, not requested in a prompt.

Exposing a field has to actually change what the action does, which is less
obvious than it sounds. Actions read their inputs in two ways. Most take
expressions over the automation's data and resolve them at run time; the model
actions and the human-in-the-loop pause take literal values and read them
straight from their stored configuration. A value from a model is a literal
either way, so it goes to both places: as an override that skips resolution for
the first kind, and as configuration for the second. Reaching only one of them
is a quiet failure rather than a loud one — the argument is validated, accepted,
and then the action does what the editor configured instead, which looks like
success.

Getting it wrong is recoverable
-------------------------------

A model producing a wrong argument is ordinary behaviour, not an emergency. It
misreads a field, invents a plausible name, sends a date in the wrong format.
If each of those failed the automation, agents would be too brittle to use.

So validation failures are returned to the model as an observation it can act
on: *that argument was not accepted, here is why*. The next turn usually gets it
right. The automation only fails when something genuinely cannot proceed —
which is a different situation, and worth being able to tell apart.

The same reasoning runs one level down. When a model emits arguments that are
not valid JSON at all, the LLM layer turns them into an empty argument set
rather than raising, precisely so they meet the schema check and come back as a
correctable error.

What this does not protect against
----------------------------------

Validation constrains the *arguments*. It says nothing about whether the tool
should have been called.

A tool that deletes records, given valid arguments, deletes records. If a model
can be steered into calling it, argument validation will not object — the call
is well-formed. That is a different problem, addressed by different machinery:
which tools an agent may call at all, whether a call needs a human to approve it
before it runs, and the budgets that stop an agent doing something a great many
times. Those are covered in the phase 1 plan under governance, and they are not
substitutes for one another.

The short version: the schema shapes what a model is likely to ask for.
Validation decides what it is allowed to have. Authorization decides whether it
should have been asking.

How an agent is bounded
-----------------------

Those three are now built, and it is worth seeing where each sits.

An agent can only call the tools declared inside it. There is no registry to
reach past and no way to name a tool it was not given — a model that tries is
told what it does have and asked again. That is authorization by construction
rather than by check.

A tool may require a person. The approval gate is the human-in-the-loop pause
the engine already had, so a tool call awaiting approval appears in the same
*Open tasks* list as any other waiting step, showing the arguments the model
chose. Actions whose effects cannot be taken back default to requiring it: the
conservative default can be relaxed by whoever adds the tool, and the opposite
mistake cannot be undone.

And an agent is bounded in four independent ways — turns, tool calls, tokens,
and wall clock — because they fail differently. Turns catch a model that will
not conclude; tool calls catch one that thrashes between them; tokens catch a
conversation growing faster than it progresses; the deadline catches everything
else. Reaching any of them **fails** the run. An agent that stopped quietly
would return a confident partial answer, and nothing about it would say it was
partial.

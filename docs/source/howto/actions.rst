Configuring actions
===================

Actions are the workhorses of an automation: each action plugin consumes the
current item, performs its work, and saves declared results into named fields.

Each instance owns one item. Use **Use data from** to select a source and
**Save result in** to select or create a destination. **Replace value** replaces
that field; **Append to list** appends one result. Other fields are preserved.
See :doc:`../glossary` and :doc:`../explanation/reading-an-automation`.

Uses and Produces
------------------

Action forms use consistent **Uses** and **Produces** sections. **Uses** chooses
input values and sources. **Produces** shows the result structure and where to
save each result. Actions without either say **No inputs needed** or **No fields produced**.
Execution settings, such as model limits, remain separate.

The automation page's title block repeats **Uses** and **Produces** in small,
human-readable text. **Uses** comes from its trigger schemas. **Produces**
follows its selected public output fields; when none are selected, the summary
says that the complete final item is returned. This is declaration metadata,
never a preview of values from a run.

Fixed actions describe their result structure read-only: changing the type in
an editor could not change what the action actually returns. **Ask a Model**
uses the shared schema editor for its configurable **Output shape**. Its field
table supports simple types; use the JSON fallback for nested structures.
The shape describes the answer, not its destination. A shape containing
``score`` saved in ``assessment`` exposes ``assessment.score``.

The input picker lists known nested fields and their producers, with warnings
for later steps and fields that may be absent. It does not guarantee that a
branch or tool has run. Open the trigger's **Automation fields** section for
the complete declaration overview. Save changes to refresh that overview.

How the options are generated
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The controls in **Uses** come from the action's declared input form. Each form
field becomes an action setting. By default, text fields use the common value
syntax described below. A **Use data from** picker inserts
``{{ field.name }}``; in a textarea it inserts at the cursor. Actions whose inputs
must remain typed—model selectors, yes/no fields, JSON editors, and other
choices—opt out of conversion and retain the widgets declared by their form.

The picker builds its options from declarations in the current trigger path:

* fields in the trigger's starting-data schema;
* destination fields declared by other actions, including known nested paths;
* any destination schema attached to those fields; and
* ``loop.entry`` and ``loop.index`` inside loops.

Each option shows its known type and source. Outputs from actions earlier in
the editor are marked as earlier; outputs from later actions are offered but
warn that they may not exist yet. Branches and tools can also leave declared
fields absent. The action's own outputs are excluded, preventing an accidental
self-reference. List-entry paths containing ``[]`` are informative catalogue
entries rather than selectable values; use ``{{ loop.entry }}`` to read them.

The controls in **Produces** come from the action's declared *result names*.
For example, Send Email declares one result named ``delivery``. The editor
therefore generates one **Save result in** row for ``delivery``. Its destination
defaults to ``delivery`` but can be replaced with any new or existing root field
name. Existing field names are suggestions, not a closed list. The adjacent
choice selects **Replace value** or **Append to list**. Clearing a destination
records that result without writing it into the item.

A result schema supplies the read-only type and nested names shown above those
destination rows. It does not generate values and does not change what the
action returns. **Ask a Model** is the exception where the author controls the
result structure: its **Output shape** uses the same schema editor as trigger
data schemas, and its result is still assigned a destination separately.

All these options are derived from trigger schemas, action definitions, saved
bindings, and model metadata. Opening an action never runs the automation,
queries execution traces, reads completed item values, or infers a schema from
sample data.

Values and data references
--------------------------

Every editable text input uses one literal-first syntax:

* ``welcome`` is the fixed text ``welcome`` — quotes are not needed;
* ``{{ subject }}`` reads the ``subject`` field from the current item;
* ``Welcome {{ customer.name }}`` inserts data into surrounding text;
* ``{{ 42 }}``, ``{{ true }}``, ``{{ false }}`` and ``{{ null }}`` produce
  native number, boolean and null values when they occupy the whole input.

A whole ``{{ ... }}`` keeps the referenced value's type, including objects and
lists. Once it is mixed with other text, the result is always text. Paths use
dots and numeric list indexes, for example ``{{ records.0.email }}``. There is
no implicit ``data`` wrapper around the current item. To write the characters
``{{`` without starting a reference, escape them as ``\{{``.

Missing data references fail the action. This makes a typo or an action running
before its producer visible in the execution trace instead of silently turning
into an empty string. Typed controls such as model choices, yes/no fields, JSON
schemas and dates retain their own editor and do not use this text syntax.

Send Email
----------

Sends one email per item using Django's email framework — any configured
``EMAIL_BACKEND`` (SMTP, SES, anymail, ...) works.

============================ ==========================================================
Field                        Meaning
============================ ==========================================================
Email Subject                e.g. ``Welcome!`` or ``{{ subject }}``
Email Body                   e.g. ``Hello {{ first_name }}!``
Body format                  optional; *HTML* sends markup plus a plain-text part
Recipient Email              e.g. ``{{ email }}`` or ``info@example.com``
Sender Email                 optional; defaults to ``DEFAULT_FROM_EMAIL``
============================ ==========================================================

Set **Body format** to *HTML* and the message goes out with both parts: the
markup for clients that render it, and a plain-text version derived from it for
those that do not. Deriving it rather than asking for the body twice is
deliberate — an editor made to write every message twice writes the second one
badly, and a mail with no text part arrives blank for anyone whose client will
not render markup.

The named result ``delivery`` contains ``sent`` and ``recipient`` and is saved
to the ``delivery`` field by default. A delivery exception fails the action.
Use **For each** to send messages for a list stored in the item.

Create / Update / Query Records
-------------------------------

Interact with Django models. For safety, only models listed in the
``AUTOMATION_ALLOWED_MODELS`` setting are available:

.. code-block:: python

    AUTOMATION_ALLOWED_MODELS = ["auth.User", "myapp.Lead"]

- **Create Record** — creates one instance per item from a JSON *field
  mapping* whose values use the same syntax, e.g.
  ``{"email": "{{ user.email }}", "source": "automation"}``. Outputs each item
  plus ``created_id`` (the named result is ``id``).
- **Update Records** — per item, updates instances matching the *filters*
  mapping (for example ``{"email": "{{ user.email }}"}``) with
  the *field mapping* values. Refuses to run without filters. Outputs each
  item plus ``updated_count`` (the named result is ``count``).
- **Query Records** — returns a list of matching records as the ``records``
  result, saved in the ``records`` field. No matches produces an empty list.
  ``pk`` is always included. Supports ``fields``, ``order_by`` and ``limit``
  (hard cap 1000). The incoming item is preserved.

LLM Prompt
----------

Runs a prompt against a large language model. Provider-independent via
`LiteLLM <https://docs.litellm.ai/>`_ — install the optional dependency:

.. code-block:: bash

    pip install djangocms-automation[llm]

Everything that talks to a language model lives in its own app, so add it to
``INSTALLED_APPS`` alongside the main one:

.. code-block:: python

    INSTALLED_APPS = [
        ...
        "djangocms_automation",
        "djangocms_automation.ai",
    ]

.. note::
   The LLM Prompt plugin is registered by that app, so without the line the
   plugin is not offered in the editor at all.

Keeping it separate is deliberate: a project that never calls a model does not
install ``litellm`` — around fifty packages, including the AWS SDK — and does
not load the app at all.

Configure the models automations may use (LiteLLM model strings,
``<provider>/<model>``) and store an API key per provider under
*Automations → Secrets* in the admin:

.. code-block:: python

    AUTOMATION_LLM_MODELS = [
        ("anthropic/claude-opus-4-8", "Claude Opus"),
        ("openai/gpt-4.1", "GPT-4.1"),
    ]
    AUTOMATION_LLM_DEFAULT = "anthropic/claude-opus-4-8"  # optional preselect

Each entry is the model string a provider is asked for and the label an editor
picks from — the label is where a project says which model is for what. A bare
string is still accepted and labels itself.

Fields:

- **Model** — one of ``AUTOMATION_LLM_MODELS``.
- **Answer format** (optional) — asks for plain text, Markdown or HTML.
  Appended to the instructions; steering rather than a guarantee.
- **System prompt** (optional) and **Prompt** — both accept text with data references.
- **Output JSON schema** (optional) — constrains the response to valid
  JSON. The answer, whether object or array, is saved in the ``answer`` field.
  Without a schema, that field contains ``text``, ``model``, ``turns``, and
  ``usage``. Read text with ``answer.text``. Object schemas must set
  ``"additionalProperties": false``.

Rate limits pause the action and it is retried automatically by the
``runautomations`` cron command (up to 5 attempts); other provider errors
fail the run with the error recorded on the action.

A reply the provider cut short — at the model's token limit, or through a
content filter — fails the action rather than becoming the automation's data.
Such a reply reads like a whole answer and simply stops, so nothing downstream
would recognise it as partial.

Tool calling
~~~~~~~~~~~~

:func:`djangocms_automation.ai.llm.complete` also accepts a conversation and a set
of tools, which is what the agent work in phase 1 is built on::

    result = complete(
        model="anthropic/claude-opus-4-8",
        messages=conversation,
        tools=[tool.to_wire() for tool in tools],
        timeout=120,
    )
    if result.wants_tools:
        for call in result.tool_calls:
            ...  # call.name, call.arguments — untrusted until validated

``prompt`` and ``messages`` are alternatives: the first is the single-turn form
this action uses, the second carries an agent's conversation so far, including
the assistant's earlier tool requests and the results that came back.

Two behaviours are deliberate. Giving tools to a model that cannot use them
raises :class:`~djangocms_automation.ai.llm.LLMToolsUnsupported` rather than
quietly dropping them — a model whose tools were ignored writes confident prose
instead of doing the work, which is far harder to diagnose. And arguments that
are not valid JSON become an empty set rather than an error, so the tool's own
schema check reports something the model can correct instead of failing the run.

Always pass ``timeout`` when an agent is driving: without one a hung provider
call holds its worker, and its action's lease, until something else gives up.


-------------

Pauses the automation until a permitted user resumes it. Configure an
optional **note** shown to the resuming user and optional
**required permissions** (comma-separated ``app_label.codename``).

Open tasks are listed in the admin at *Execution Instances → Open tasks*
(``/admin/djangocms_automation/automationinstance/open-tasks/``), where
permitted users (and superusers) can resume them.

Wait for User
-------------

Pauses the automation until a permitted user resumes it. Configure an
optional **note** shown to the resuming user and optional
**required permissions** (comma-separated ``app_label.codename``).

Open tasks are listed in the admin at *Execution Instances → Open tasks*
(``/admin/djangocms_automation/automationinstance/open-tasks/``), where
permitted users (and superusers) can resume them. An agent's tool call waiting
for approval appears in the same list; see :doc:`agents`.

Writing your own action
-----------------------

Subclass :class:`~djangocms_automation.models.BaseActionPluginModel` as a
proxy model and override ``perform``:

.. code-block:: python

    from djangocms_automation.models import BaseActionPluginModel

    class SlackActionModel(BaseActionPluginModel):
        class Meta:
            proxy = True

        default_outputs = {"delivery": {"field": "slack_delivery"}}

        def perform(self, context, inputs):
            receipt = notify_slack(inputs["channel"], inputs["message"])
            return {"delivery": receipt}

Then register a CMS plugin subclassing
``djangocms_automation.cms_plugins.ActionPlugin`` with a ``data_form``
declaring the inputs. Raise
``djangocms_automation.engine.ActionPause(until=...)`` to pause and retry
later; raise any other exception to fail the run.

The engine resolves and records ``inputs`` before calling ``perform``. Return
only named result values, never a replacement item or a list of items.
``context.item`` is a private snapshot; mutating it does not write fields.
Use ``context.record(kind, payload)`` for additional observations. Executor
working state belongs in ``action.scratch`` and must be saved through
``execution.save_working_state`` so a stale worker cannot overwrite it.

``default_outputs`` maps result names to destination fields. The editor stores
overrides in ``outputs``, for example
``{"delivery": {"field": "deliveries", "mode": "append"}}``. A result mapped
to ``null`` is recorded but not saved in the item; clear its destination in the
editor to select this. Returning no results (``{}``) performs no field writes.

Declare a ``result_schemas`` mapping alongside ``default_outputs``, for example
``{"delivery": {"type": "object", "properties": {"sent": {"type": "boolean"}},
"additionalProperties": False}}``. This supplies read-only result types and
nested field names to the editor. Override ``get_result_schemas()`` when the
shape depends on configuration, using definitions and model metadata only;
never query records or execution payloads to infer it. Unknown shapes use
``{}`` and are labelled dynamic. These declarations describe results; they do
not add runtime validation or change the action's implementation.

The editor derives its controls from these declarations as follows:

``data_form``
   Supplies the fields in **Uses**. Ordinary character fields become
   literal-first value inputs with a field picker. Textareas use the same syntax
   and insert a selection at the cursor. Set ``convert_data_form = False`` to
   retain the form's typed widgets; preserved textareas still allow data
   insertion. Names in
   ``literal_fields`` are treated as literal values during execution and are
   excluded from data-path discovery. JSON mappings whose values use the common
   syntax declare their names in ``expression_mappings``; individual values are resolved at
   runtime but are not turned into separate picker controls.

``default_outputs``
   Supplies the result names, initial destinations, and write modes used to
   generate **Save result in** rows. Instance-level ``outputs`` stores the
   editor's overrides. Returning a key that was not declared does not silently
   create an editor option; action authors should keep returned result names and
   declarations in agreement.

``result_schemas`` / ``get_result_schemas()``
   Supplies read-only result types and nested paths. The mapping is keyed by the
   same result names as ``default_outputs``. Use ``get_result_schemas()`` for
   configuration-dependent shapes, such as selected Django model fields. It
   must use definitions or model metadata, never database rows or run data.

The destination suggestions passed to output rows are generated by the same
field catalogue used by input pickers. Destinations themselves are stable root
identifiers; nested result fields appear beneath that root after mapping. Thus a
result ``answer`` with child ``score``, saved in ``assessment``, is offered to
later actions as ``assessment.score`` rather than ``answer.score``.

Declare typed literal configuration names in ``literal_fields`` and mappings
of value templates in ``expression_mappings``. An explicit optional binding can use
``{"field": "customer", "path": "email", "default": "fallback@example.com"}``.
Without a default, missing paths in configured values fail.

``AutomationContent.data_fields`` maps stable keys to labels and optional JSON
schemas, for example ``{"count": {"label": "Count", "schema": {"type": "integer"}}}``.
This catalogue is derived from trigger schemas and action output mappings and
is not edited separately in the Automation form. Configured destination schemas
are frozen with the run and validated on writes.

``AutomationContent.output_fields`` is edited as **Produces**, using fields from
that catalogue. An empty list means **Produce the complete final item**. Choose
a smaller public result only when another automation or integration consumes
it; the selection does not change the data available to actions within the
automation. ``loop`` is reserved for execution scope.

Custom executor code must remain compatible with its recorded
``execution_version`` (default 1). Increment that version for incompatible
changes; old definitions then fail explicitly rather than silently executing
different semantics. Store credential references, not secret values, in action
configuration: configuration is part of the retained execution definition.

Configuring actions
===================

Actions are the workhorses of an automation: each action plugin consumes the
current item, performs its work, and saves declared results into named fields.

Each instance owns one item. Use **Use data from** to select a source and
**Save result in** to select or create a destination. **Replace value** replaces
that field; **Append to list** appends one result. Other fields are preserved.
See :doc:`../glossary` and :doc:`../explanation/reading-an-automation`.

Expressions and templates
-------------------------

Most action inputs are **expressions**: a number literal (``42``), a quoted
string literal (``"info@django-cms.org"``), or a dotted path into the current
item (``user.email``). Lists live in fields (``records.0.email``). There is
no implicit ``data`` variable or batch wrapper.

Multi-line inputs (email bodies, LLM prompts) are **templates**: free text
with ``{{ dotted.path }}`` substitution against the current item.

Send Email
----------

Sends one email per item using Django's email framework — any configured
``EMAIL_BACKEND`` (SMTP, SES, anymail, ...) works.

============================ ==========================================================
Field                        Meaning
============================ ==========================================================
Email Subject (expression)   e.g. ``"Welcome!"`` or ``subject``
Email Body (template)        e.g. ``Hello {{ first_name }}!``
Body format                  optional; *HTML* sends markup plus a plain-text part
Recipient Email (expression) e.g. ``email`` or ``"info@example.com"``
Sender Email (expression)    optional; defaults to ``DEFAULT_FROM_EMAIL``
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
  mapping* of model fields to expressions, e.g.
  ``{"email": "user.email", "source": "'automation'"}``. Outputs each item
  plus ``created_id`` (the named result is ``id``).
- **Update Records** — per item, updates instances matching the *filters*
  mapping (lookups to expressions, e.g. ``{"email": "user.email"}``) with
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
- **System prompt** (template, optional) and **Prompt** (template).
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
optional **note** (template) shown to the resuming user and optional
**required permissions** (comma-separated ``app_label.codename``).

Open tasks are listed in the admin at *Execution Instances → Open tasks*
(``/admin/djangocms_automation/automationinstance/open-tasks/``), where
permitted users (and superusers) can resume them.

Wait for User
-------------

Pauses the automation until a permitted user resumes it. Configure an
optional **note** (template) shown to the resuming user and optional
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

Declare literal configuration names in ``literal_fields`` and mappings of
expressions in ``expression_mappings``. An explicit optional binding can use
``{"field": "customer", "path": "email", "default": "fallback@example.com"}``.
Without a default, missing paths in action expressions or templates fail.

``AutomationContent.data_fields`` maps stable keys to labels and optional JSON
schemas, for example ``{"count": {"label": "Count", "schema": {"type": "integer"}}}``.
Configured destination schemas are frozen with the run and validated on writes.
``AutomationContent.output_fields`` selects public output fields; an empty list
returns the complete final item. ``loop`` is reserved for execution scope.

Custom executor code must remain compatible with its recorded
``execution_version`` (default 1). Increment that version for incompatible
changes; old definitions then fail explicitly rather than silently executing
different semantics. Store credential references, not secret values, in action
configuration: configuration is part of the retained execution definition.

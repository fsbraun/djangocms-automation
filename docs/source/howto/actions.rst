Configuring actions
===================

Actions are the workhorses of an automation: each action plugin consumes the
current data rows, performs a side effect, and produces the rows for the
next step.

Expressions and templates
-------------------------

Most action inputs are **expressions**: a number literal (``42``), a quoted
string literal (``"info@django-cms.org"``), or a dotted path into the current
data row (``user.email``). The full row list is available as ``data``
(``data.0.email`` addresses the first row).

Multi-line inputs (email bodies, LLM prompts) are **templates**: free text
with ``{{ dotted.path }}`` substitution against the current row.

Send Email
----------

Sends one email per data row using Django's email framework — any configured
``EMAIL_BACKEND`` (SMTP, SES, anymail, ...) works.

============================ ==========================================================
Field                        Meaning
============================ ==========================================================
Email Subject (expression)   e.g. ``"Welcome!"`` or ``subject``
Email Body (template)        e.g. ``Hello {{ first_name }}!``
Recipient Email (expression) e.g. ``email`` or ``"info@example.com"``
Sender Email (expression)    optional; defaults to ``DEFAULT_FROM_EMAIL``
============================ ==========================================================

Each output row gains a ``_mail`` entry (``sent``, ``recipient``,
``error``). If **all** rows fail, the action (and the run) fails; partial
failures complete with per-row status.

Create / Update / Query Records
-------------------------------

Interact with Django models. For safety, only models listed in the
``AUTOMATION_ALLOWED_MODELS`` setting are available:

.. code-block:: python

    AUTOMATION_ALLOWED_MODELS = ["auth.User", "myapp.Lead"]

- **Create Record** — creates one instance per row from a JSON *field
  mapping* of model fields to expressions, e.g.
  ``{"email": "user.email", "source": "'automation'"}``. Outputs each row
  plus ``_created_id``.
- **Update Records** — per row, updates instances matching the *filters*
  mapping (lookups to expressions, e.g. ``{"email": "user.email"}``) with
  the *field mapping* values. Refuses to run without filters. Outputs each
  row plus ``_updated`` (count).
- **Query Records** — runs once per step; emits one row per matched
  instance (``pk`` always included). Supports ``fields``, ``order_by`` and
  ``limit`` (hard cap 1000).

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
        "anthropic/claude-opus-4-8",
        "openai/gpt-4.1",
    ]
    AUTOMATION_LLM_DEFAULT = "anthropic/claude-opus-4-8"  # optional preselect

Fields:

- **Model** — one of ``AUTOMATION_LLM_MODELS``.
- **System prompt** (template, optional) and **Prompt** (template).
- **Output JSON schema** (optional) — constrains the response to valid
  JSON. A JSON *array* response becomes the new data rows; an *object*
  becomes a single row. Without a schema, one
  ``{"text", "model", "turns", "usage"}`` row is emitted. Object schemas must set
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

        def perform(self, action, rows):
            inputs = self.resolve_inputs(rows[0] if rows else {}, rows)
            notify_slack(inputs["channel"], inputs["message"])
            return rows

Then register a CMS plugin subclassing
``djangocms_automation.cms_plugins.ActionPlugin`` with a ``data_form``
declaring the inputs. Raise
``djangocms_automation.engine.ActionPause(until=...)`` to pause and retry
later; raise any other exception to fail the run.

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
  ``{"text", "model", "usage"}`` row is emitted. Object schemas must set
  ``"additionalProperties": false``.

Rate limits pause the action and it is retried automatically by the
``runautomations`` cron command (up to 5 attempts); other provider errors
fail the run with the error recorded on the action.

Wait for User
-------------

Pauses the automation until a permitted user resumes it. Configure an
optional **note** (template) shown to the resuming user and optional
**required permissions** (comma-separated ``app_label.codename``).

Open tasks are listed in the admin at *Execution Instances → Open tasks*
(``/admin/djangocms_automation/automationinstance/open-tasks/``), where
permitted users (and superusers) can resume them.

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

AI Reference
============

Everything that talks to a language model lives in
``djangocms_automation.ai``, an installed app in its own right. The package
imports from the core engine; the core engine never imports from it. That rule
is checked by a test, and it is what keeps ``litellm`` — and the roughly fifty
packages it brings — optional for projects that never call a model.

The LLM wrapper
---------------

.. automodule:: djangocms_automation.ai.llm
   :members:
   :undoc-members:
   :show-inheritance:

The tool contract
-----------------

.. automodule:: djangocms_automation.ai.tools
   :members:
   :undoc-members:
   :show-inheritance:

Tools are derived from an action's ``data_form``:
:func:`~djangocms_automation.ai.tools.schema_from_form` builds the JSON Schema
from the fields it already declares, and
:func:`~djangocms_automation.ai.tools.validate_arguments` runs a model's
arguments back through the same form. ``include`` limits both to the inputs a
tool exposes; everything else stays bound by the editor.

For why those are two separate steps rather than one, and what each does and
does not guarantee, see :doc:`../explanation/tools-and-trust`.

Agents
------

For building one in the editor, see :doc:`../howto/agents`.

.. automodule:: djangocms_automation.ai.models
   :members:
   :undoc-members:
   :show-inheritance:

.. automodule:: djangocms_automation.ai.state
   :members:
   :undoc-members:
   :show-inheritance:

.. automodule:: djangocms_automation.ai.budget
   :members:
   :undoc-members:
   :show-inheritance:

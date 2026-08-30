AI Reference
============

Everything that talks to a language model lives in
``djangocms_automation.ai``, an installed app in its own right. The package
imports from the core engine; the core engine never imports from it. That rule
is checked by a test, and it is what keeps ``litellm`` — and the roughly fifty
packages it brings — optional for projects that never call a model.

The tool contract is the exception, and deliberately so: it lives in core,
because an action being called as a tool is core behaviour and has no provider
dependency. Only the wrapper that speaks to a provider is here.

The AI step
-----------

.. automodule:: djangocms_automation.ai.step
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

The LLM wrapper
---------------

.. automodule:: djangocms_automation.ai.llm
   :members:
   :undoc-members:
   :show-inheritance:

Trying it without a provider
----------------------------

.. automodule:: djangocms_automation.ai.dummy
   :members:
   :undoc-members:

The tool contract
-----------------

Lives in core. An action and a tool are the same object seen from two sides, so
what makes an action callable by a model is not in this package.

.. automodule:: djangocms_automation.tools
   :members:
   :undoc-members:
   :show-inheritance:

.. automodule:: djangocms_automation.tool_mixin
   :members:
   :undoc-members:
   :show-inheritance:

Tools are derived from an action's ``data_form``:
:func:`~djangocms_automation.tools.schema_from_form` builds the JSON Schema from
the fields it already declares, and
:func:`~djangocms_automation.tools.validate_arguments` runs a model's arguments
back through the same form. Which inputs are included is the editor's wiring;
everything else stays bound.

For why those are two separate steps, and what each does and does not
guarantee, see :doc:`../explanation/tools-and-trust`. For building one in the
editor, see :doc:`../howto/agents`.

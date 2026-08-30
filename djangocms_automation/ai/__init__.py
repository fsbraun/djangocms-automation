"""Everything in this package that talks to a language model.

Kept together, and kept one-directional: this package imports from the core
engine, and the core engine never imports from it. That is what allows the AI
surface to be lifted into a separately installable ``contrib`` app later as a
directory rename rather than an untangling — and, in the meantime, what lets a
project that will never use an LLM leave the optional dependency uninstalled and
this app out of ``INSTALLED_APPS``.

The rule is checked by a test, not just stated here.
"""

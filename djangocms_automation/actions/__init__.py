"""Concrete action implementations.

Each action is a proxy model of
:class:`djangocms_automation.models.BaseActionPluginModel` overriding
:meth:`perform`, paired with a CMS plugin (registered in
:mod:`djangocms_automation.cms_plugins`) that declares the action's
``data_form`` inputs.
"""

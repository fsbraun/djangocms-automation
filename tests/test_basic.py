import pytest


@pytest.mark.django_db
def test_basic_setup():
    """Test that the basic Django setup works"""
    from django.contrib.auth.models import User

    # Create a test user
    user = User.objects.create_user(username="testuser", password="testpass")
    assert user.username == "testuser"
    assert User.objects.count() == 1


def test_package_version():
    """Test that the package version is accessible"""
    import djangocms_automation

    assert hasattr(djangocms_automation, "__version__")
    assert djangocms_automation.__version__ == "0.1.0"


def test_a_plugin_renders_as_its_name_in_a_template():
    """``str()`` on a plugin has to return a string, not a lazy one.

    Plugin names are translated, so ``name`` is a proxy that only becomes text
    when something asks for it. ``CMSPluginBase.__str__`` hands that proxy back
    unresolved, and Python rejects a ``__str__`` that does not return ``str``.

    Nothing in this package stringifies a plugin, which is why this went
    unnoticed: the admin change form does. ``{{ fieldset.name|default:adminform.model_admin }}``
    falls back to the plugin whenever a fieldset has no name of its own — which
    is every plugin's first fieldset by default — and the add-plugin view dies
    with ``__str__ returned non-string``.
    """
    from cms.plugin_pool import plugin_pool
    from django.template import Context, Template

    from djangocms_automation.cms_plugins import AutomationPlugin

    for plugin_class in plugin_pool.get_all_plugins():
        if not issubclass(plugin_class, AutomationPlugin):
            continue
        instance = plugin_class(plugin_class.model, None)
        assert isinstance(str(instance), str), f"{plugin_class.__name__} does not stringify"
        rendered = Template("{{ plugin }}").render(Context({"plugin": instance}))
        assert rendered == str(plugin_class.name)

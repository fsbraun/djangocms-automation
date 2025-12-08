import os
import sys
from datetime import datetime

from djangocms_automation import __version__


# Add project root to sys.path so autodoc can import the package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

project = "djangocms-automation"
author = ""
release = __version__

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.todo",
    "sphinx.ext.autosummary",
]

# When building autodoc, many modules import Django/CMS and expect
# a configured Django settings module. It's usually easier to mock
# these heavy external dependencies for the docs build.
autodoc_mock_imports = [
    "django",
    "django.conf",
    "django.db",
    "django.db.models",
    "django.apps",
    "django.contrib",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.admin",
    "cms",
    "cms.plugin_pool",
    "cms.plugin_base",
    "cms.api",
    "djangocms_form_builder",
    "djangocms_text",
    "jsonschema",
    "django_tasks",
    "django.tasks",
]

autosummary_generate = True
autodoc_typehints = "description"
autodoc_member_order = "bysource"

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_theme_options = {
    # Furo defaults are usually fine; customize here if desired
}
html_static_path = ["_static"]

# Intersphinx mappings (useful for cross-referencing Python/Django)
intersphinx_mapping = {
    "python": ("https://docs.python.org/3/", None),
    "django": ("https://docs.djangoproject.com/en/stable/", None),
}

# General substitutions
rst_prolog = f"""
.. |project| replace:: {project}
.. |year| replace:: {datetime.utcnow().year}
"""

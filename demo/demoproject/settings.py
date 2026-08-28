"""Settings for the demo project.

Deliberately close to a real deployment: a durable task backend, a worker
process, and a scheduler — not the inline execution the test suite uses. The
database is SQLite for convenience; nothing else here is a shortcut.
"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_KEY = "demo-only-not-a-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "demoproject.urls"
SITE_ID = 1

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "cms",
    "menus",
    "treebeard",
    "sekizai",
    "djangocms_versioning",
    "djangocms_form_builder",
    "djangocms_automation",
    "demoproject",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "cms.middleware.user.CurrentUserMiddleware",
    "cms.middleware.page.CurrentPageMiddleware",
    "cms.middleware.toolbar.ToolbarMiddleware",
    "cms.middleware.language.LanguageCookieMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "sekizai.context_processors.sekizai",
                "cms.context_processors.cms_settings",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.path.join(BASE_DIR, "demo.sqlite3"),
    }
}

LANGUAGE_CODE = "en"
LANGUAGES = (("en", "English"),)
CMS_LANGUAGES = {
    1: [{"code": "en", "name": "English", "public": True}],
    "default": {"hide_untranslated": False},
}
CMS_TEMPLATES = (("base.html", "Default template"),)
CMS_CONFIRM_VERSION4 = True

USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "static")

# --- Automation configuration -------------------------------------------
# A durable backend, so enqueue and execution are genuinely separate. Run
# `python manage.py runworker` alongside `runserver`.
TASKS = {
    "default": {
        "BACKEND": "djangocms_automation.backends.DatabaseBackend",
        "QUEUES": ["default"],
    }
}

# Model actions are deny-all by default; the demo opts its own models in.
AUTOMATION_ALLOWED_MODELS = [
    "demoproject.Order",
    "demoproject.Article",
]

AUTOMATION_LEASE_SECONDS = 300
AUTOMATION_ACTION_TIMEOUT = 900
AUTOMATION_TIMER_CATCHUP = 1

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = "automation@example.com"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "djangocms_automation": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

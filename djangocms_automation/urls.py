"""URL patterns for djangocms_automation.

Include in the project urlconf to enable inbound webhooks::

    urlpatterns = [
        ...
        path("automation/", include("djangocms_automation.urls")),
    ]
"""

from django.urls import path

from .views import WebhookView

app_name = "djangocms_automation"

urlpatterns = [
    path("webhook/<str:token>/", WebhookView.as_view(), name="webhook"),
]

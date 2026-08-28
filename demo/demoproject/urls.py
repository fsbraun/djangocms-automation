from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("automation/", include("djangocms_automation.urls")),
    path("", include("cms.urls")),
]

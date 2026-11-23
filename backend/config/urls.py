from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("apps.core.urls")),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("django-admin/", admin.site.urls),
]

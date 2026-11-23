from django.contrib import admin
from django.urls import include, path
from django_otp.admin import OTPAdminSite

# Django admin requires a 2FA-verified session too; a password alone must never reach customer data.
admin.site.__class__ = OTPAdminSite

urlpatterns = [
    path("", include("apps.core.urls")),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("django-admin/", admin.site.urls),
]

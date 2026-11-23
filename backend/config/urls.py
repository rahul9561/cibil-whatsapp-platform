from django.contrib import admin
from django.urls import include, path
from django_otp.admin import OTPAdminSite

from apps.messaging.webhooks import whatsapp_webhook
from apps.payments.webhooks import razorpay_webhook

# Django admin requires a 2FA-verified session too; a password alone must never reach customer data.
admin.site.__class__ = OTPAdminSite

urlpatterns = [
    path("", include("apps.core.urls")),
    path("api/v1/", include("config.api_urls")),
    # Unauthenticated by design: trust comes from the signature check, not from a session.
    path("webhooks/razorpay", razorpay_webhook, name="webhook-razorpay"),
    path("webhooks/whatsapp", whatsapp_webhook, name="webhook-whatsapp"),
    path("django-admin/", admin.site.urls),
]

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import Role
from apps.core.admin import ReadOnlyModelAdmin
from apps.core.testing.factories import (
    ConsentFactory,
    FollowUpFactory,
    PaymentFactory,
    UserFactory,
)

pytestmark = pytest.mark.django_db

PASSWORD = "test-password-123"


def verified_superuser_client():
    user = UserFactory(email="root@example.com", role=Role.SUPER_ADMIN, is_staff=True, is_superuser=True)
    device = TOTPDevice.objects.create(user=user, name="x", confirmed=True)
    client = Client()
    client.post(
        "/api/v1/auth/login", {"email": user.email, "password": PASSWORD}, content_type="application/json"
    )
    token = str(totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)).zfill(6)
    resp = client.post("/api/v1/auth/2fa/verify", {"token": token}, content_type="application/json")
    assert resp.status_code == 200
    return client


def test_admin_refuses_password_only_session():
    UserFactory(email="root@example.com", role=Role.SUPER_ADMIN, is_staff=True, is_superuser=True)
    client = Client()
    client.post(
        "/api/v1/auth/login",
        {"email": "root@example.com", "password": PASSWORD},
        content_type="application/json",
    )
    resp = client.get("/django-admin/")
    assert resp.status_code == 302 and "/login/" in resp["Location"]


def test_every_admin_page_loads_read_only_without_plaintext_pii():
    consent = ConsentFactory(customer__wa_id="919876543210", customer__full_name="Rahul Sharma")
    customer = consent.customer
    customer.set_pan("ABCDE1234F")
    customer.save()
    PaymentFactory(payment_link__report_request__customer=customer)
    FollowUpFactory(report_request__customer__wa_id="919800000001")
    client = verified_superuser_client()

    for model, model_admin in admin.site._registry.items():
        opts = model._meta
        changelist = client.get(reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist"))
        assert changelist.status_code == 200, opts.label
        if model.__module__.startswith("apps."):
            assert isinstance(model_admin, ReadOnlyModelAdmin), f"{opts.label} admin must be read-only"
        obj = model._default_manager.first()
        if obj is not None and opts.app_label not in {"auth", "otp_totp", "accounts"}:
            detail = client.get(reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk]))
            assert detail.status_code == 200, opts.label
            for plain in ("9876543210", "Rahul Sharma", "ABCDE1234F"):
                assert plain not in detail.content.decode(), (opts.label, plain)
        for plain in ("9876543210", "Rahul Sharma", "ABCDE1234F"):
            assert plain not in changelist.content.decode(), (opts.label, plain)

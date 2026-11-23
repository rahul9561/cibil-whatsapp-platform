"""Endpoint x role matrix: every role gets exactly what the capability matrix grants, nothing more."""

import pytest
from django.test import Client

from apps.accounts.capabilities import role_has
from apps.accounts.models import Role
from apps.core.testing.api import client_for, make_request
from apps.core.testing.factories import UserFactory

pytestmark = pytest.mark.django_db

READ_ENDPOINTS = {
    "/api/v1/customers": "customer.view",
    "/api/v1/report-requests": "report.view",
    "/api/v1/payment-links": "payment.view",
    "/api/v1/payments": "payment.view",
    "/api/v1/refunds": "payment.view",
    "/api/v1/follow-ups": "followup.view",
    "/api/v1/messages": "customer.view",
    "/api/v1/users": "staff.manage",
    "/api/v1/audit-logs": "audit.view",
    "/api/v1/audit-logs/verify": "audit.view",
    "/api/v1/dashboard/summary": "dashboard.view",
    "/api/v1/analytics/funnel": "dashboard.view",
    "/api/v1/analytics/revenue": "analytics.revenue",
    "/api/v1/settings": "settings.manage",
}


@pytest.mark.parametrize("role", list(Role))
def test_read_matrix(role):
    client, _ = client_for(role)
    for url, cap in READ_ENDPOINTS.items():
        expected = 200 if role_has(role, cap) else 403
        assert client.get(url).status_code == expected, (role, url)


def test_anonymous_gets_nothing():
    client = Client()
    for url in [*READ_ENDPOINTS, "/api/v1/schema"]:
        assert client.get(url).status_code == 403, url


def test_password_only_session_is_refused_for_2fa_roles():
    client = Client()
    client.force_login(UserFactory(role=Role.SUPER_ADMIN))
    for url in READ_ENDPOINTS:
        assert client.get(url).status_code == 403, url


@pytest.mark.parametrize("role", list(Role))
def test_write_matrix(role):
    """403 exactly when the role lacks the capability (otherwise any non-403 outcome is fine here)."""
    paid = make_request(paid=True, wa_id="919811100001")
    unpaid = make_request(wa_id="919811100002")
    if role == Role.AGENT:  # agents act only on their assigned items; test the capability, not the scope
        client, user = client_for(role)
        for req in (paid, unpaid):
            req.followup.assigned_to = user
            req.followup.save()
    else:
        client, user = client_for(role)
    payment = paid.payment_links.get().payments.get()
    j = {"content_type": "application/json"}
    actions = [
        (
            "customer.reveal_pii",
            "post",
            f"/api/v1/customers/{unpaid.customer_id}/reveal",
            {"fields": ["pan"], "reason": "verification"},
        ),
        ("message.send", "post", f"/api/v1/customers/{unpaid.customer_id}/messages", {"text": "Hello"}),
        ("report.download", "get", f"/api/v1/report-requests/{paid.pk}/document", None),
        ("payment.resend_link", "post", f"/api/v1/report-requests/{unpaid.pk}/payment-links/resend", {}),
        (
            "refund.request",
            "post",
            f"/api/v1/payments/{payment.pk}/refunds",
            {"amount_paise": 100, "reason": "x"},
        ),
        ("followup.manage", "post", f"/api/v1/follow-ups/{unpaid.followup.pk}/notes", {"body": "called"}),
        (
            "staff.manage",
            "post",
            "/api/v1/users",
            {"email": "new@example.com", "role": "agent", "password": "a-long-password-123"},
        ),
        ("report.manage", "post", f"/api/v1/report-requests/{unpaid.pk}/cancel", {"reason": "test"}),
    ]
    for cap, method, url, body in actions:
        resp = getattr(client, method)(url, body, **j) if body is not None else getattr(client, method)(url)
        if role_has(role, cap):
            assert resp.status_code != 403, (role, cap, resp.status_code)
        else:
            assert resp.status_code == 403, (role, cap, resp.status_code)

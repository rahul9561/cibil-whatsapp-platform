"""Behaviour of the admin API beyond permissions: scoping, masking, state rules, audit, side effects."""

from datetime import timedelta

import pytest
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.conversations.models import Conversation
from apps.core.testing.api import client_for, make_request
from apps.core.testing.factories import UserFactory
from apps.followups.models import FollowUpStatus, Reminder, ReminderStatus
from apps.payments.models import PaymentLinkStatus, PaymentStatus, RefundStatus
from apps.reports.models import ReportStatus

pytestmark = pytest.mark.django_db
J = {"content_type": "application/json"}


def results(resp):
    body = resp.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


# --- customers -------------------------------------------------------------------------------------------


def test_customer_list_is_masked_and_reveal_is_audited():
    req = make_request(generated=False, wa_id="919876543210")
    admin, _ = client_for(Role.ADMIN)
    listing = admin.get("/api/v1/customers")
    text = listing.content.decode()
    assert "9876543210" not in text and "ABCDE1234F" not in text and "Rahul Sharma" not in text
    row = results(listing)[0]
    assert (
        row["phone"] == "+91 98******10"
        and row["pan"] == "AB******4F"
        and row["open_request"] == req.public_ref
    )

    resp = admin.post(
        f"/api/v1/customers/{req.customer_id}/reveal",
        {"fields": ["pan", "phone"], "reason": "customer call"},
        **J,
    )
    assert resp.json() == {"pan": "ABCDE1234F", "phone": "919876543210"}
    entry = AuditLog.objects.get(action="customer.pii_revealed")
    assert entry.changes["fields"] == ["pan", "phone"] and entry.changes["reason"] == "customer call"


def test_customer_search_by_phone_and_pan_uses_blind_index():
    make_request(generated=False, wa_id="919876500001", pan="AAAAA1111A")
    make_request(generated=False, wa_id="919876500002", pan="BBBBB2222B")
    admin, _ = client_for(Role.ADMIN)
    assert len(results(admin.get("/api/v1/customers?phone=+91 98765 00001"))) == 1
    assert len(results(admin.get("/api/v1/customers?pan=bbbbb2222b"))) == 1
    assert len(results(admin.get("/api/v1/customers?pan=ZZZZZ9999Z"))) == 0


def test_manual_message_respects_the_24h_window():
    req = make_request()
    admin, user = client_for(Role.ADMIN)
    url = f"/api/v1/customers/{req.customer_id}/messages"
    assert admin.post(url, {"text": "Hello"}, **J).status_code == 201
    Conversation.objects.filter(customer=req.customer).update(
        last_inbound_at=timezone.now() - timedelta(days=2)
    )
    resp = admin.post(url, {"text": "Hello"}, **J)
    assert resp.status_code == 422 and resp.json()["code"] == "outside_service_window"
    assert admin.post(url, {"template_key": "payment_reminder"}, **J).status_code == 201
    assert req.customer.messages.filter(sent_by=user).count() == 2


def test_manual_template_parameters_are_built_by_the_server(settings):
    """D2: staff choose only the template; reference, price and link come from the open report."""
    from apps.messaging.gateway.mock import outbox
    from apps.payments.services import format_rupees

    req = make_request()
    Conversation.objects.filter(customer=req.customer).update(
        last_inbound_at=timezone.now() - timedelta(days=2)
    )
    admin, user = client_for(Role.ADMIN)
    url = f"/api/v1/customers/{req.customer_id}/messages"
    resp = admin.post(
        url, {"template_key": "payment_reminder", "template_params": ["FAKE", "₹1", "https://evil"]}, **J
    )
    assert resp.status_code == 201
    link = req.payment_links.get()  # the existing link is reused, not a second one created
    sent = outbox()[-1]
    assert sent["template"] == settings.WHATSAPP_TEMPLATES["payment_reminder"]
    assert sent["params"] == [req.public_ref, format_rupees(link.amount_paise), link.short_url]
    assert link.sent_count == 2  # first automatic send + this one

    assert admin.post(url, {"template_key": "payment_link"}, **J).status_code == 201
    assert outbox()[-1]["template"] == settings.WHATSAPP_TEMPLATES["payment_link"]
    assert outbox()[-1]["params"][0] == req.public_ref


def test_manual_template_without_a_payable_report_is_a_conflict():
    req = make_request(generated=False)  # still collecting details: nothing to pay for
    admin, _ = client_for(Role.ADMIN)
    url = f"/api/v1/customers/{req.customer_id}/messages"
    resp = admin.post(url, {"template_key": "payment_reminder"}, **J)
    assert resp.status_code == 409
    assert not req.customer.messages.filter(type="template").exists()
    paid = make_request(paid=True, wa_id="919811100099")
    resp = admin.post(f"/api/v1/customers/{paid.customer_id}/messages", {"template_key": "payment_link"}, **J)
    assert resp.status_code == 409  # already paid: nothing awaiting payment


def test_manual_reminder_respects_opt_out():
    req = make_request()
    req.customer.opted_out_at = timezone.now()
    req.customer.save(update_fields=["opted_out_at"])
    admin, _ = client_for(Role.ADMIN)
    resp = admin.post(
        f"/api/v1/customers/{req.customer_id}/messages", {"template_key": "payment_reminder"}, **J
    )
    assert resp.status_code == 422 and resp.json()["code"] == "customer_opted_out"


def test_manual_message_rejects_text_and_template_together():
    req = make_request()
    admin, _ = client_for(Role.ADMIN)
    url = f"/api/v1/customers/{req.customer_id}/messages"
    assert admin.post(url, {"text": "Hi", "template_key": "payment_link"}, **J).status_code == 400


# --- agent scoping ---------------------------------------------------------------------------------------


def test_agents_only_see_their_assigned_work():
    agent_client, agent = client_for(Role.AGENT)
    mine = make_request(assigned_to=agent, wa_id="919811100010")
    other = make_request(wa_id="919811100011")
    assert [r["public_ref"] for r in results(agent_client.get("/api/v1/report-requests"))] == [
        mine.public_ref
    ]
    assert [f["report_ref"] for f in results(agent_client.get("/api/v1/follow-ups"))] == [mine.public_ref]
    assert len(results(agent_client.get("/api/v1/customers"))) == 1
    assert agent_client.get(f"/api/v1/report-requests/{other.pk}").status_code == 404
    assert agent_client.get(f"/api/v1/customers/{other.customer_id}/timeline").status_code == 404
    admin, _ = client_for(Role.ADMIN)
    assert len(results(admin.get("/api/v1/report-requests"))) == 2


# --- report requests -------------------------------------------------------------------------------------


def test_unpaid_filter_and_payment_status():
    unpaid = make_request(wa_id="919811100020")
    make_request(paid=True, wa_id="919811100021")
    admin, _ = client_for(Role.ADMIN)
    rows = results(admin.get("/api/v1/report-requests?unpaid=true"))
    assert [r["public_ref"] for r in rows] == [unpaid.public_ref]
    assert rows[0]["payment_status"] == "awaiting_payment"
    assert results(admin.get("/api/v1/report-requests?unpaid=false"))[0]["payment_status"] == "paid"


def test_cancel_closes_link_and_followup_but_not_after_payment():
    req = make_request(wa_id="919811100030")
    admin, _ = client_for(Role.ADMIN)
    resp = admin.post(f"/api/v1/report-requests/{req.pk}/cancel", {"reason": "customer asked"}, **J)
    assert resp.status_code == 200 and resp.json()["report_status"] == ReportStatus.CANCELLED
    req.refresh_from_db()
    assert req.payment_links.get().status == PaymentLinkStatus.CANCELLED
    assert req.followup.status == FollowUpStatus.CLOSED_MANUAL
    assert not req.followup.reminders.filter(status=ReminderStatus.SCHEDULED).exists()

    paid = make_request(paid=True, wa_id="919811100031")
    assert admin.post(f"/api/v1/report-requests/{paid.pk}/cancel", {"reason": "x"}, **J).status_code == 409


def test_document_download_is_audited_and_not_cached():
    req = make_request(paid=True)
    admin, _ = client_for(Role.ADMIN)
    resp = admin.get(f"/api/v1/report-requests/{req.pk}/document")
    assert resp.status_code == 200 and resp.content.startswith(b"%PDF-")
    assert resp["Cache-Control"] == "no-store, private"
    assert f"credit-report-{req.public_ref}.pdf" in resp["Content-Disposition"]
    assert AuditLog.objects.filter(action="report.document_downloaded", object_id=str(req.pk)).exists()
    no_doc = make_request(generated=False, wa_id="919811100040")
    assert admin.get(f"/api/v1/report-requests/{no_doc.pk}/document").status_code == 409


def test_retry_only_when_there_is_something_to_retry():
    admin, _ = client_for(Role.ADMIN)
    req = make_request()
    assert admin.post(f"/api/v1/report-requests/{req.pk}/retry").status_code == 409


def test_resend_payment_link_increments_count():
    req = make_request()
    admin, user = client_for(Role.ADMIN)
    resp = admin.post(f"/api/v1/report-requests/{req.pk}/payment-links/resend")
    assert resp.status_code == 200 and resp.json()["sent_count"] == 2
    assert req.messages.filter(sent_by=user).count() == 1


# --- refunds ---------------------------------------------------------------------------------------------


def test_refund_four_eyes_and_processing():
    req = make_request(paid=True)
    payment = req.payment_links.get().payments.get()
    admin, _ = client_for(Role.ADMIN)
    finance, _ = client_for(Role.FINANCE)

    too_much = admin.post(
        f"/api/v1/payments/{payment.pk}/refunds", {"amount_paise": 99999, "reason": "x"}, **J
    )
    assert too_much.status_code == 422

    refund = admin.post(
        f"/api/v1/payments/{payment.pk}/refunds", {"amount_paise": 9900, "reason": "dup"}, **J
    )
    assert refund.status_code == 201
    rid = refund.json()["id"]
    second = finance.post(f"/api/v1/payments/{payment.pk}/refunds", {"amount_paise": 1, "reason": "x"}, **J)
    assert second.status_code == 422  # full amount already in flight

    resp = finance.post(f"/api/v1/refunds/{rid}/approve", {"note": "ok"}, **J)
    assert resp.status_code == 200 and resp.json()["status"] == RefundStatus.PROCESSED
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.REFUNDED and payment.amount_refunded_paise == 9900
    assert finance.post(f"/api/v1/refunds/{rid}/approve", {}, **J).status_code == 422  # already decided


def test_refund_self_approval_refused():
    req = make_request(paid=True)
    payment = req.payment_links.get().payments.get()
    finance, _ = client_for(Role.FINANCE)
    rid = finance.post(
        f"/api/v1/payments/{payment.pk}/refunds", {"amount_paise": 100, "reason": "x"}, **J
    ).json()["id"]
    resp = finance.post(f"/api/v1/refunds/{rid}/approve", {}, **J)
    assert resp.status_code == 422 and resp.json()["code"] == "refund_self_approval"


# --- follow-ups ------------------------------------------------------------------------------------------


def test_followup_rules_for_agents_and_admins():
    agent_client, agent = client_for(Role.AGENT)
    admin, _ = client_for(Role.ADMIN)
    req = make_request()
    fid = req.followup.pk
    url = f"/api/v1/follow-ups/{fid}"

    assert admin.patch(url, {"assigned_to": str(agent.pk), "priority": 3}, **J).status_code == 200
    assert agent_client.patch(url, {"priority": 1}, **J).status_code == 403  # agents can't re-prioritise

    note = agent_client.post(
        f"{url}/notes", {"body": "No answer", "channel": "call", "outcome": "no_answer"}, **J
    )
    assert note.status_code == 201
    assert agent_client.get(url).json()["status"] == FollowUpStatus.IN_PROGRESS

    past = (timezone.now() - timedelta(hours=1)).isoformat()
    assert agent_client.post(f"{url}/reminders", {"scheduled_for": past}, **J).status_code == 400
    future = (timezone.now() + timedelta(minutes=30)).isoformat()
    rem = agent_client.post(f"{url}/reminders", {"scheduled_for": future}, **J)
    assert rem.status_code == 201
    assert agent_client.delete(f"/api/v1/reminders/{rem.json()['id']}").status_code == 204
    assert Reminder.objects.get(pk=rem.json()["id"]).status == ReminderStatus.CANCELLED

    assert agent_client.patch(url, {"status": "closed_lost"}, **J).status_code == 400  # reason required
    assert agent_client.patch(url, {"status": "closed_paid", "close_reason": "x"}, **J).status_code == 400
    resp = agent_client.patch(url, {"status": "closed_lost", "close_reason": "not interested"}, **J)
    assert resp.status_code == 200 and resp.json()["closed_at"]
    assert not Reminder.objects.filter(follow_up_id=fid, status=ReminderStatus.SCHEDULED).exists()
    assert agent_client.patch(url, {"status": "in_progress"}, **J).status_code == 409  # closed


def test_followup_filters():
    admin, me = client_for(Role.ADMIN)
    a = make_request(wa_id="919811100050")
    make_request(wa_id="919811100051")
    a.followup.assigned_to = me
    a.followup.save()
    assert len(results(admin.get("/api/v1/follow-ups?assignee=me"))) == 1
    assert len(results(admin.get("/api/v1/follow-ups?assignee=unassigned"))) == 1
    assert len(results(admin.get("/api/v1/follow-ups?open=true"))) == 2
    a.followup.__class__.objects.filter(pk=a.followup.pk).update(
        next_reminder_at=timezone.now() - timedelta(1)
    )
    assert len(results(admin.get("/api/v1/follow-ups?overdue=true"))) == 1


# --- staff -----------------------------------------------------------------------------------------------


def test_staff_management_guards():
    root, me = client_for(Role.SUPER_ADMIN)
    weak = root.post("/api/v1/users", {"email": "a@example.com", "role": "agent", "password": "123"}, **J)
    assert weak.status_code == 400
    created = root.post(
        "/api/v1/users", {"email": "B@Example.com", "role": "agent", "password": "a-long-password-123"}, **J
    )
    assert created.status_code == 201 and created.json()["email"] == "b@example.com"
    assert (
        root.post(
            "/api/v1/users",
            {"email": "b@example.com", "role": "agent", "password": "a-long-password-123"},
            **J,
        ).status_code
        == 400
    )

    assert root.patch(f"/api/v1/users/{me.pk}", {"is_active": False}, **J).status_code == 400
    assert root.patch(f"/api/v1/users/{me.pk}", {"role": "admin"}, **J).status_code == 400

    other_root = UserFactory(role=Role.SUPER_ADMIN)
    assert root.patch(f"/api/v1/users/{other_root.pk}", {"role": "admin"}, **J).status_code == 200
    me.__class__.objects.filter(pk=other_root.pk).update(role=Role.SUPER_ADMIN, is_active=True)

    target = UserFactory(role=Role.FINANCE)
    TOTPDevice.objects.create(user=target, name="x", confirmed=True)
    target_client, _ = client_for(user=target)
    assert target_client.get("/api/v1/payments").status_code == 200
    resp = root.post(f"/api/v1/users/{target.pk}/reset-2fa")
    assert resp.status_code == 200 and resp.json()["two_factor_enrolled"] is False
    assert target_client.get("/api/v1/payments").status_code == 403  # session ended


# --- audit, dashboard, analytics, schema -----------------------------------------------------------------


def test_audit_log_listing_and_verify():
    make_request(paid=True)
    auditor, _ = client_for(Role.AUDITOR)
    rows = results(auditor.get("/api/v1/audit-logs?action=payment"))
    assert {r["action"] for r in rows} >= {"payment.captured", "payment_link.created"}
    assert auditor.get("/api/v1/audit-logs/verify").json()["ok"] is True


def test_dashboard_funnel_and_revenue():
    make_request(paid=True, wa_id="919811100060")
    make_request(wa_id="919811100061")
    make_request(generated=False, wa_id="919811100062")
    admin, _ = client_for(Role.ADMIN)
    summary = admin.get("/api/v1/dashboard/summary").json()
    assert summary["today"]["started"] == 3 and summary["today"]["generated"] == 2
    assert summary["today"]["payments"] == 1 and summary["today"]["revenue_paise"] == 9900
    assert summary["unpaid"] == {"count": 1, "value_paise": 9900}
    assert summary["followups"]["open"] == 1

    stages = admin.get("/api/v1/analytics/funnel").json()["stages"]
    assert stages == {
        "started": 3,
        "consented": 2,
        "otp_verified": 2,
        "generated": 2,
        "paid": 1,
        "delivered": 1,
    }

    today = timezone.localdate().isoformat()
    rows = admin.get(f"/api/v1/analytics/revenue?from={today}&to={today}").json()
    assert rows == [
        {"period": today, "payments": 1, "gross_paise": 9900, "refunds_paise": 0, "net_paise": 9900}
    ]
    csv = admin.get(f"/api/v1/analytics/revenue?from={today}&to={today}&export=csv")
    assert csv["Content-Type"] == "text/csv" and f"{today},1,9900,0,9900" in csv.content.decode()
    assert admin.get("/api/v1/analytics/revenue?from=2026-02-01&to=2026-01-01").status_code == 400


def test_agent_dashboard_hides_revenue():
    agent, _ = client_for(Role.AGENT)
    assert "revenue_paise" not in agent.get("/api/v1/dashboard/summary").json()["today"]


def test_schema_served_to_staff():
    admin, _ = client_for(Role.ADMIN)
    resp = admin.get("/api/v1/schema")
    assert resp.status_code == 200 and b"/api/v1/report-requests" in resp.content


def test_assignee_picker_for_admins_only():
    admin, _ = client_for(Role.ADMIN)
    agent_client, agent = client_for(Role.AGENT)
    UserFactory(role=Role.AUDITOR)
    UserFactory(role=Role.AGENT, is_active=False)
    rows = admin.get("/api/v1/follow-ups/assignees").json()
    assert {r["role"] for r in rows} <= {"agent", "admin", "super_admin"}
    assert str(agent.pk) in {r["id"] for r in rows} and set(rows[0]) == {"id", "email", "role"}
    assert agent_client.get("/api/v1/follow-ups/assignees").status_code == 403

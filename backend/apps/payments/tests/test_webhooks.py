"""Razorpay webhook receiver and processing, end to end on the mock gateway."""

import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.core.integrations import TransientError
from apps.core.models import WebhookEvent, WebhookProcessingStatus
from apps.core.testing.api import make_request
from apps.core.testing.factories import UserFactory
from apps.followups.models import FollowUpStatus
from apps.messaging.gateway.mock import outbox
from apps.payments.gateway.mock import MockPaymentGateway, simulate_payment
from apps.payments.models import PaymentLinkStatus, PaymentStatus, RefundStatus
from apps.payments.services import RefundService
from apps.payments.tasks import reconcile_payment_links, retry_failed_webhook_events
from apps.reports.models import DeliveryStatus

pytestmark = pytest.mark.django_db
SECRET = "whsec_test_secret"
URL = "/webhooks/razorpay"


@pytest.fixture(autouse=True)
def webhook_secret(settings):
    settings.RAZORPAY_WEBHOOK_SECRET = SECRET


def event(name, *, link=None, payment=None, refund=None):
    payload = {}
    if link:
        payload["payment_link"] = {
            "entity": {**link, "customer": {"contact": "+919876543210", "email": "a@b.com", "name": "Rahul"}}
        }
    if payment:
        payload["payment"] = {
            "entity": {
                **payment,
                "email": "a@b.com",
                "contact": "+919876543210",
                "vpa": "rahul@okbank",
                "card": {"last4": "1111"},
                "notes": {"x": "y"},
            }
        }
    if refund:
        payload["refund"] = {"entity": refund}
    return {"entity": "event", "event": name, "payload": payload, "created_at": 1900000000}


def post(client, body: dict, *, event_id="evt_1", secret=SECRET, raw: bytes | None = None):
    data = raw if raw is not None else json.dumps(body).encode()
    sig = hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()
    return client.post(
        URL,
        data,
        content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE=sig,
        HTTP_X_RAZORPAY_EVENT_ID=event_id,
    )


def test_paid_webhook_delivers_report(django_capture_on_commit_callbacks):
    req = make_request()
    link = req.payment_links.get()
    simulate_payment(link.razorpay_link_id)
    body = event(
        "payment_link.paid",
        link={"id": link.razorpay_link_id, "status": "paid"},
        payment={"id": "pay_x", "amount": 9900, "currency": "INR", "status": "captured"},
    )
    with django_capture_on_commit_callbacks(execute=True):
        resp = post(Client(enforce_csrf_checks=True), body)
    assert resp.status_code == 200 and resp.json() == {"status": "accepted"}

    ev = WebhookEvent.objects.get()
    assert ev.processing_status == WebhookProcessingStatus.PROCESSED and ev.attempts == 1
    link.refresh_from_db()
    req.refresh_from_db()
    assert link.status == PaymentLinkStatus.PAID
    assert req.delivery_status == DeliveryStatus.SENT
    assert req.followup.status == FollowUpStatus.CLOSED_PAID
    stored = json.dumps(ev.payload)
    for secret_value in ("rahul@okbank", "a@b.com", "9876543210", "1111", "Rahul"):
        assert secret_value not in stored


def test_duplicate_event_is_acknowledged_and_processed_once(django_capture_on_commit_callbacks):
    req = make_request()
    link = req.payment_links.get()
    simulate_payment(link.razorpay_link_id)
    body = event("payment_link.paid", link={"id": link.razorpay_link_id})
    client = Client()
    with django_capture_on_commit_callbacks(execute=True):
        assert post(client, body).json() == {"status": "accepted"}
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        assert post(client, body).json() == {"status": "duplicate"}
    assert callbacks == []
    assert WebhookEvent.objects.count() == 1
    assert len([m for m in outbox() if m["kind"] == "document"]) == 1


def test_invalid_signature_is_rejected_and_audited():
    body = event("payment_link.paid", link={"id": "plink_x"})
    resp = post(Client(), body, secret="attacker-guess")
    assert resp.status_code == 400
    assert not WebhookEvent.objects.exists()
    assert AuditLog.objects.filter(action="webhook.signature_invalid").exists()


def test_signature_is_checked_on_raw_bytes():
    raw = b'{"event": "payment_link.paid",  "payload": {}}'  # odd spacing: re-serialising would break it
    assert post(Client(), {}, raw=raw).status_code == 200
    tampered = Client().post(
        URL,
        raw.replace(b"paid", b"expired"),
        content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE=hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
    )
    assert tampered.status_code == 400


def test_malformed_and_oversized_and_wrong_method():
    assert post(Client(), {}, raw=b"not json").status_code == 400
    assert post(Client(), {}, raw=b"[1, 2]").status_code == 400
    assert post(Client(), {}, raw=b"x" * (256 * 1024 + 1)).status_code == 413
    assert Client().get(URL).status_code == 405


def test_event_for_unknown_link_or_type_is_ignored(django_capture_on_commit_callbacks):
    client = Client()
    with django_capture_on_commit_callbacks(execute=True):
        post(client, event("payment_link.paid", link={"id": "plink_not_ours"}), event_id="e1")
        post(client, event("payment.captured", payment={"id": "pay_1"}), event_id="e2")
        post(client, event("order.paid"), event_id="e3")
    assert set(WebhookEvent.objects.values_list("processing_status", flat=True)) == {
        WebhookProcessingStatus.IGNORED
    }


def test_expired_link_event_marks_link_expired(django_capture_on_commit_callbacks):
    req = make_request()
    link = req.payment_links.get()
    with mock.patch.object(MockPaymentGateway, "fetch_payment_link") as fetch:
        fetch.return_value = mock.Mock(status="expired")
        with django_capture_on_commit_callbacks(execute=True):
            post(Client(), event("payment_link.expired", link={"id": link.razorpay_link_id}))
    link.refresh_from_db()
    assert link.status == PaymentLinkStatus.EXPIRED and link.open_lock is None


def test_mismatched_payment_is_held_for_review(django_capture_on_commit_callbacks):
    req = make_request()
    link = req.payment_links.get()
    simulate_payment(link.razorpay_link_id, amount_paise=link.amount_paise + 1)
    with django_capture_on_commit_callbacks(execute=True):
        post(Client(), event("payment_link.paid", link={"id": link.razorpay_link_id}))
    ev = WebhookEvent.objects.get()
    assert ev.processing_status == WebhookProcessingStatus.FAILED and ev.last_error == "payment_mismatch"
    link.refresh_from_db()
    assert link.status == PaymentLinkStatus.ACTIVE


def test_pending_refund_is_finished_by_webhook(django_capture_on_commit_callbacks, monkeypatch):
    monkeypatch.setattr(MockPaymentGateway, "refund_status", "pending")
    req = make_request(paid=True)
    payment = req.payment_links.get().payments.get()
    svc = RefundService()
    refund = svc.request(payment.pk, amount_paise=9900, reason="dup", by_user=UserFactory())
    svc.decide(refund.pk, approve=True, by_user=UserFactory())
    refund = svc.process(refund.pk)
    assert refund.status == RefundStatus.PROCESSING and refund.razorpay_refund_id

    with django_capture_on_commit_callbacks(execute=True):
        post(
            Client(),
            event(
                "refund.processed",
                refund={"id": refund.razorpay_refund_id, "status": "processed", "amount": 9900},
            ),
        )
    refund.refresh_from_db()
    payment.refresh_from_db()
    assert refund.status == RefundStatus.PROCESSED
    assert payment.status == PaymentStatus.REFUNDED and payment.amount_refunded_paise == 9900
    with django_capture_on_commit_callbacks(execute=True):  # replay with a new id: no double counting
        post(
            Client(),
            event("refund.processed", refund={"id": refund.razorpay_refund_id, "status": "processed"}),
            event_id="evt_2",
        )
    payment.refresh_from_db()
    assert payment.amount_refunded_paise == 9900


def test_refund_retry_after_timeout_never_refunds_twice(monkeypatch):
    req = make_request(paid=True)
    payment = req.payment_links.get().payments.get()
    svc = RefundService()
    refund = svc.request(payment.pk, amount_paise=500, reason="x", by_user=UserFactory())
    svc.decide(refund.pk, approve=True, by_user=UserFactory())

    real_create = MockPaymentGateway.create_refund
    calls = []

    def create_then_time_out(self, payment_id, amount, reference):
        calls.append(reference)
        real_create(self, payment_id, amount, reference)  # the refund *was* created at the gateway...
        raise TransientError("timeout", code="razorpay_unreachable")  # ...but we never saw the response

    monkeypatch.setattr(MockPaymentGateway, "create_refund", create_then_time_out)
    with pytest.raises(TransientError):
        svc.process(refund.pk)
    refund = svc.process(refund.pk)  # the retry finds the existing refund instead of creating another
    assert len(calls) == 1
    assert refund.status == RefundStatus.PROCESSED and refund.razorpay_refund_id


def test_reconciliation_catches_missed_webhook():
    req = make_request()
    link = req.payment_links.get()
    link.__class__.objects.filter(pk=link.pk).update(created_at=timezone.now() - timedelta(minutes=10))
    simulate_payment(link.razorpay_link_id)  # paid, but no webhook ever arrives
    assert reconcile_payment_links.delay().get() == 1
    req.refresh_from_db()
    assert req.delivery_status == DeliveryStatus.SENT
    assert reconcile_payment_links.delay().get() == 0  # nothing left open


def test_stuck_events_are_requeued(django_capture_on_commit_callbacks):
    req = make_request()
    link = req.payment_links.get()
    simulate_payment(link.razorpay_link_id)
    ev = WebhookEvent.objects.create(
        source="razorpay",
        event_id="lost",
        event_type="payment_link.paid",
        signature_valid=True,
        payload={"payment_link": {"id": link.razorpay_link_id}},
    )
    WebhookEvent.objects.filter(pk=ev.pk).update(received_at=timezone.now() - timedelta(minutes=10))
    assert retry_failed_webhook_events.delay().get() == 1
    ev.refresh_from_db()
    assert ev.processing_status == WebhookProcessingStatus.PROCESSED
    req.refresh_from_db()
    assert req.delivery_status == DeliveryStatus.SENT


def test_link_unknown_to_the_gateway_is_cancelled_once_not_retried_forever():
    from django.core.cache import cache

    from apps.payments.gateway.mock import _link_key
    from apps.payments.tasks import confirm_payment_task

    req = make_request()
    link = req.payment_links.get()
    link.__class__.objects.filter(pk=link.pk).update(created_at=timezone.now() - timedelta(minutes=10))
    cache.delete(_link_key(link.razorpay_link_id))  # gateway has no record of it
    assert confirm_payment_task.delay(link.razorpay_link_id).get() == "error:link_not_found"
    link.refresh_from_db()
    assert link.status == PaymentLinkStatus.CANCELLED
    assert AuditLog.objects.filter(action="payment_link.missing_at_gateway").count() == 1
    assert reconcile_payment_links.delay().get() == 0  # no longer polled every 10 minutes


def test_other_permanent_errors_are_logged_without_changing_the_link(monkeypatch):
    from apps.core.integrations import PermanentError
    from apps.payments.tasks import confirm_payment_task

    req = make_request()
    link = req.payment_links.get()

    def refuse(self, link_id):
        raise PermanentError("bad key", code="razorpay_bad_request")

    monkeypatch.setattr(MockPaymentGateway, "fetch_payment_link", refuse)
    assert confirm_payment_task.delay(link.razorpay_link_id).get() == "error:razorpay_bad_request"
    link.refresh_from_db()
    assert link.status == PaymentLinkStatus.ACTIVE  # could be a key mix-up: never cancel a real link on it

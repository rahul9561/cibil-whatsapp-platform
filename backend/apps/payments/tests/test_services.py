from datetime import timedelta

import pytest
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.conversations.models import Conversation
from apps.core.testing.factories import PaymentLinkFactory, ReportRequestFactory
from apps.payments.gateway.mock import simulate_payment
from apps.payments.models import PaymentLink, PaymentLinkStatus
from apps.payments.services import NotPayable, PaymentService, format_rupees
from apps.reports.models import ReportStatus

pytestmark = pytest.mark.django_db


def generated_request():
    req = ReportRequestFactory()
    ReportRequestFactory._meta.model.objects.filter(pk=req.pk).update(report_status=ReportStatus.GENERATED)
    req.refresh_from_db()
    Conversation.objects.create(customer=req.customer, last_inbound_at=timezone.now())
    return req


def test_link_only_for_generated_reports():
    with pytest.raises(NotPayable):
        PaymentService().get_or_create_link(ReportRequestFactory().pk)


def test_valid_link_is_reused():
    req = generated_request()
    svc = PaymentService()
    first = svc.get_or_create_link(req.pk)
    again = svc.get_or_create_link(req.pk)
    assert first.pk == again.pk and first.status == PaymentLinkStatus.ACTIVE
    assert first.reference_id == f"{req.public_ref}-1"


def test_nearly_expired_link_is_replaced():
    req = generated_request()
    svc = PaymentService()
    first = svc.get_or_create_link(req.pk)
    PaymentLink.objects.filter(pk=first.pk).update(expires_at=timezone.now() + timedelta(minutes=30))
    second = svc.get_or_create_link(req.pk)
    first.refresh_from_db()
    assert second.pk != first.pk and second.attempt == 2
    assert first.status == PaymentLinkStatus.CANCELLED and second.status == PaymentLinkStatus.ACTIVE


def test_pending_link_retry_is_idempotent_at_gateway():
    req = generated_request()
    pending = PaymentLinkFactory(report_request=req, expires_at=timezone.now() + timedelta(hours=72))
    link = PaymentService().get_or_create_link(req.pk)
    assert link.pk == pending.pk and link.status == PaymentLinkStatus.ACTIVE


def test_no_new_link_after_payment():
    req = generated_request()
    svc = PaymentService()
    link = svc.get_or_create_link(req.pk)
    simulate_payment(link.razorpay_link_id)
    svc.confirm_link_payment(link.razorpay_link_id)
    with pytest.raises(NotPayable):
        svc.get_or_create_link(req.pk)


def test_resend_uses_a_new_idempotency_key_each_time():
    req = generated_request()
    svc = PaymentService()
    link = svc.get_or_create_link(req.pk)
    svc.send_link(link)
    svc.send_link(link)
    link.refresh_from_db()
    assert link.sent_count == 2
    assert req.messages.count() == 2


def test_paying_two_links_for_one_report_is_flagged():
    req = generated_request()
    svc = PaymentService()
    first = svc.get_or_create_link(req.pk)
    PaymentLink.objects.filter(pk=first.pk).update(expires_at=timezone.now() + timedelta(minutes=5))
    second = svc.get_or_create_link(req.pk)
    simulate_payment(first.razorpay_link_id)  # customer paid the old link before it was cancelled
    simulate_payment(second.razorpay_link_id)
    # The first link was cancelled locally; confirming it must still record the money.
    PaymentLink.objects.filter(pk=first.pk).update(status=PaymentLinkStatus.ACTIVE)
    svc.confirm_link_payment(first.razorpay_link_id)
    svc.confirm_link_payment(second.razorpay_link_id)
    assert AuditLog.objects.filter(action="payment.duplicate").count() == 1


@pytest.mark.parametrize(("paise", "text"), [(9900, "₹99"), (9950, "₹99.50"), (100, "₹1")])
def test_format_rupees(paise, text):
    assert format_rupees(paise) == text

import pytest
from django.db import IntegrityError, transaction

from apps.core.testing.factories import (
    PaymentFactory,
    PaymentLinkFactory,
    ReportRequestFactory,
    UserFactory,
)
from apps.payments.models import PaymentLinkStatus, Refund, RefundStatus

pytestmark = pytest.mark.django_db


def test_one_open_link_per_request_and_new_link_after_expiry():
    req = ReportRequestFactory()
    first = PaymentLinkFactory(report_request=req, attempt=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentLinkFactory(report_request=req, attempt=2)

    first.status = PaymentLinkStatus.EXPIRED
    first.save(update_fields=["status"])
    second = PaymentLinkFactory(report_request=req, attempt=2)
    assert second.open_lock == req.pk


def test_reference_id_and_attempt_are_unique():
    link = PaymentLinkFactory()
    link.status = PaymentLinkStatus.CANCELLED
    link.save()
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentLinkFactory(report_request=link.report_request, attempt=1, reference_id="other-ref")
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentLinkFactory(reference_id=link.reference_id)


def test_razorpay_ids_unique_but_nullable():
    PaymentLinkFactory(razorpay_link_id=None)
    PaymentLinkFactory(razorpay_link_id=None)  # many links not yet created at Razorpay
    PaymentLinkFactory(razorpay_link_id="plink_A")
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentLinkFactory(razorpay_link_id="plink_A")

    PaymentFactory(razorpay_payment_id="pay_A")
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentFactory(razorpay_payment_id="pay_A")


def test_refunded_amount_cannot_exceed_payment():
    payment = PaymentFactory(amount_paise=9900)
    payment.amount_refunded_paise = 9900
    payment.save()
    assert payment.refundable_paise == 0
    payment.amount_refunded_paise = 9901
    with pytest.raises(IntegrityError), transaction.atomic():
        payment.save()


def test_refund_four_eyes_rule():
    payment = PaymentFactory()
    requester, approver = UserFactory(), UserFactory()
    refund = Refund.objects.create(payment=payment, amount_paise=100, reason="dup", requested_by=requester)
    refund.approved_by = requester
    refund.status = RefundStatus.APPROVED
    with pytest.raises(IntegrityError), transaction.atomic():
        refund.save()
    refund.approved_by = approver
    refund.save()


def test_amounts_must_be_positive():
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentLinkFactory(amount_paise=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        Refund.objects.create(
            payment=PaymentFactory(), amount_paise=0, reason="x", requested_by=UserFactory()
        )

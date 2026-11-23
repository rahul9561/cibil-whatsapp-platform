import re

import pytest
from django.db import IntegrityError, transaction

from apps.core.testing.factories import CustomerFactory, ReportRequestFactory
from apps.reports.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_REPORT_STATUSES,
    DeliveryStatus,
    InvalidTransition,
    ReportRequest,
    ReportStatus,
)

pytestmark = pytest.mark.django_db

HAPPY_PATH = [
    ReportStatus.CONSENTED,
    ReportStatus.OTP_SENT,
    ReportStatus.OTP_VERIFIED,
    ReportStatus.FETCHING,
    ReportStatus.GENERATED,
]


def advance(req, *statuses):
    for s in statuses:
        req.transition_to(s)
        req.save()
    return req


def test_public_ref_format_and_uniqueness():
    refs = {ReportRequestFactory().public_ref for _ in range(20)}
    assert len(refs) == 20
    assert all(re.fullmatch(r"CR-[2-9A-HJKMNP-Z]{8}", r) for r in refs)


def test_only_one_open_request_per_customer():
    customer = CustomerFactory()
    ReportRequestFactory(customer=customer)
    with pytest.raises(IntegrityError), transaction.atomic():
        ReportRequestFactory(customer=customer)
    ReportRequestFactory()  # another customer is unaffected


@pytest.mark.parametrize("terminal", sorted(TERMINAL_REPORT_STATUSES - {ReportStatus.NO_HIT}))
def test_terminal_request_releases_the_lock(terminal):
    customer = CustomerFactory()
    first = ReportRequestFactory(customer=customer)
    first.transition_to(terminal)
    first.save(update_fields=["report_status"])  # lock is added to update_fields automatically
    first.refresh_from_db()
    assert first.active_lock is None
    ReportRequestFactory(customer=customer)


def test_generated_but_undelivered_still_blocks_then_delivery_releases():
    customer = CustomerFactory()
    req = advance(ReportRequestFactory(customer=customer), *HAPPY_PATH)
    assert req.generated_at is not None
    assert req.active_lock == customer.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        ReportRequestFactory(customer=customer)

    req.delivery_status = DeliveryStatus.DELIVERED
    req.save(update_fields=["delivery_status"])
    req.refresh_from_db()
    assert req.active_lock is None
    ReportRequestFactory(customer=customer)


def test_no_hit_releases_lock():
    customer = CustomerFactory()
    req = advance(ReportRequestFactory(customer=customer), *HAPPY_PATH[:4], ReportStatus.NO_HIT)
    assert req.active_lock is None


def test_invalid_transitions_are_rejected():
    req = ReportRequestFactory()
    with pytest.raises(InvalidTransition):
        req.transition_to(ReportStatus.GENERATED)  # cannot skip OTP + fetch
    advance(req, *HAPPY_PATH[:4])
    with pytest.raises(InvalidTransition):
        req.transition_to(ReportStatus.CANCELLED)  # provider call in flight
    advance(req, ReportStatus.FAILED)
    for status in ReportStatus:
        assert not req.can_transition_to(status)


def test_transition_table_covers_every_status():
    assert set(ALLOWED_TRANSITIONS) == set(ReportStatus)
    for targets in ALLOWED_TRANSITIONS.values():
        assert targets <= set(ReportStatus)


def test_failure_details_recorded():
    req = ReportRequestFactory()
    req.transition_to(ReportStatus.FAILED, failure_code="provider_timeout", failure_detail="3 retries")
    req.save()
    req.refresh_from_db()
    assert (req.failure_code, req.failure_detail) == ("provider_timeout", "3 retries")


def test_price_must_be_positive():
    with pytest.raises(IntegrityError), transaction.atomic():
        ReportRequestFactory(price_paise=0)


def test_lock_cannot_point_at_another_customer():
    req = ReportRequestFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        ReportRequest.objects.filter(pk=req.pk).update(active_lock=CustomerFactory().pk)

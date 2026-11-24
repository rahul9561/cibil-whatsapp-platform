"""The WhatsApp journey with a provider that needs no OTP (Surepass-style), plus the PAN-misuse guard."""

from datetime import timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time

from apps.conversations.models import Conversation
from apps.conversations.models import ConversationState as S
from apps.customers.models import Customer
from apps.messaging.dev import simulate_inbound
from apps.reports.maintenance import expire_stale_requests
from apps.reports.models import ReportRequest, ReportStatus

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def direct_provider(settings):
    settings.CREDIT_PROVIDER = "mock_no_otp"
    settings.PREFILL_BY_MOBILE = False  # these tests cover the typed-details path; prefill has its own tests


def say(wa, text=None, button=None):
    return simulate_inbound(wa, text=text, reply_id=button)


def texts(replies):
    return " || ".join(r.get("body", "") for r in replies)


def state(wa):
    return Conversation.objects.get(customer=Customer.objects.by_wa_id(wa).get()).state


def run_to_report(wa, pan="ABCDE1234F", name="Vishal Rathore"):
    say(wa, "hi")
    say(wa, name)
    say(wa, pan)
    say(wa, button="male")
    say(wa, button="confirm")
    return say(wa, button="agree")


def test_asks_gender_not_dob_and_never_asks_for_an_otp():
    wa = "919800011111"
    say(wa, "hi")
    say(wa, "Vishal Rathore")
    r = say(wa, "EKRPR1234F")
    assert state(wa) == S.ASK_EXTRA and "gender" in texts(r) and r[-1]["buttons"] == ["male", "female"]
    assert "choose one" in texts(say(wa, "maybe"))
    r = say(wa, button="male")
    assert state(wa) == S.CONFIRM_DETAILS
    assert "Gender: Male" in texts(r) and "Date of birth" not in texts(r)

    r = say(wa, button="confirm")
    consent = texts(r)
    assert "this WhatsApp number" in consent and "one-time password" not in consent

    r = say(wa, button="agree")
    assert "fetching your credit report" in texts(r)
    req = ReportRequest.objects.get()
    assert req.consent.consent_text_version.endswith("-direct")
    assert req.report_status == ReportStatus.GENERATED and req.otp_attempts == 0
    assert req.provider_name == "mock_no_otp" and req.pan_hash == req.customer.pan_hash
    assert req.payment_links.count() == 1
    assert state(wa) == S.AWAITING_PAYMENT
    assert Customer.objects.get().dob == ""  # not collected: the provider doesn't need it


def test_no_hit_and_provider_failure_without_otp():
    run_to_report("919800011112", pan="NOHIT0000N")
    assert ReportRequest.objects.get().report_status == ReportStatus.NO_HIT
    r = run_to_report("919800011113", pan="REJEC0000R")
    req = ReportRequest.objects.get(
        customer__wa_id_hash=Customer.objects.by_wa_id("919800011113").get().wa_id_hash
    )
    assert req.report_status == ReportStatus.FAILED and req.failure_code == "applicant_rejected"
    assert "couldn't process these details" in texts(r)


def test_same_pan_from_another_number_is_blocked():
    run_to_report("919800022221", pan="PQRST6789K")
    r = run_to_report("919800022222", pan="PQRST6789K")
    blocked = ReportRequest.objects.filter(report_status=ReportStatus.FAILED).get()
    assert blocked.failure_code == "pan_used_by_other_number"
    assert not blocked.payment_links.exists()
    assert "recently used for another report" in texts(r)
    assert state("919800022222") == S.REPORT_FAILED


def test_one_number_cannot_pull_several_pans():
    wa = "919800033331"
    run_to_report(wa, pan="AAAAA1111A")
    req = ReportRequest.objects.get()
    ReportRequest.objects.filter(pk=req.pk).update(delivery_status="sent", active_lock=None)  # finished
    say(wa, "hi")
    say(wa, button="edit")
    say(wa, "Someone Else")
    say(wa, "BBBBB2222B")
    say(wa, button="female")
    say(wa, button="confirm")
    say(wa, button="agree")
    second = ReportRequest.objects.exclude(pk=req.pk).get()
    assert second.report_status == ReportStatus.FAILED and second.failure_code == "number_used_for_other_pan"


def test_guard_window_and_switch(settings):
    with freeze_time("2026-11-01 10:00:00"):
        run_to_report("919800044441", pan="CCCCC3333C")
    with freeze_time("2026-12-05 10:00:00"):  # 34 days later: outside the 30-day window
        run_to_report("919800044442", pan="CCCCC3333C")
        assert ReportRequest.objects.filter(report_status=ReportStatus.GENERATED).count() == 2
    settings.PAN_GUARD_DAYS = 0
    run_to_report("919800044443", pan="CCCCC3333C")
    assert ReportRequest.objects.filter(report_status=ReportStatus.GENERATED).count() == 3


def test_otp_provider_is_not_subject_to_the_guard(settings):
    settings.CREDIT_PROVIDER = "mock"  # the bureau's OTP proves ownership
    from apps.core.testing.api import make_request

    make_request(wa_id="919800055551", pan="DDDDD4444D")
    make_request(wa_id="919800055552", pan="DDDDD4444D")
    assert ReportRequest.objects.filter(report_status=ReportStatus.GENERATED).count() == 2


def test_consented_request_with_lost_fetch_is_restarted_not_abandoned(monkeypatch):
    with freeze_time("2026-11-24 10:00:00") as clock:
        monkeypatch.setattr("apps.reports.tasks.fetch_report_task.delay", lambda pk: None)  # task lost
        run_to_report("919800066661")
        req = ReportRequest.objects.get()
        assert req.report_status == ReportStatus.CONSENTED
        monkeypatch.undo()
        clock.tick(timedelta(minutes=11))
        Conversation.objects.update(last_inbound_at=timezone.now() - timedelta(hours=2))
        result = expire_stale_requests()
        assert result["fetch_restarted"] == 1 and result["abandoned"] == 0
        req.refresh_from_db()
        assert req.report_status == ReportStatus.GENERATED

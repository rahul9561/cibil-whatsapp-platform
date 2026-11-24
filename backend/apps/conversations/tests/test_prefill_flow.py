"""Phone-number-only journey: consent -> bureau prefill by WhatsApp number -> "Is this you?" -> report.

Mock prefill (mock_no_otp) by the number's last digit: 9 = no record, 8 = provider down, else RAHUL SHARMA
with PAN "PFILL" + last 4 digits + "P", male.
"""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from freezegun import freeze_time

from apps.audit.models import AuditLog
from apps.conversations.models import Conversation
from apps.conversations.models import ConversationState as S
from apps.customers.models import Customer
from apps.messaging.dev import simulate_inbound
from apps.reports.maintenance import expire_stale_requests
from apps.reports.models import ReportRequest, ReportStatus

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def prefill_provider(settings):
    settings.CREDIT_PROVIDER = "mock_no_otp"
    settings.PREFILL_BY_MOBILE = True


def say(wa, text=None, button=None):
    return simulate_inbound(wa, text=text, reply_id=button)


def texts(replies):
    return " || ".join(r.get("body", "") for r in replies)


def customer(wa):
    return Customer.objects.by_wa_id(wa).get()


def state(wa):
    return Conversation.objects.get(customer=customer(wa)).state


def test_only_the_phone_number_is_needed():
    wa = "919800012341"
    r = say(wa, "Hi")
    assert state(wa) == S.AWAITING_CONSENT  # consent before any bureau lookup
    consent = texts(r)
    assert "look up the details the credit bureau links to this WhatsApp number" in consent
    assert "+91 98******41" in consent and r[-1]["buttons"] == ["agree", "decline"]
    assert customer(wa).pan == ""

    r = say(wa, button="agree")
    assert state(wa) == S.CONFIRM_DETAILS
    shown = texts(r)
    assert "We found these details" in shown and "R*** S***" in shown and "PF******1P" in shown
    assert "Gender: Male" in shown and r[-1]["buttons"] == ["confirm", "not_me"]
    assert customer(wa).pan == "" and customer(wa).full_name == ""  # not stored until confirmed

    r = say(wa, button="confirm")
    assert "fetching your credit report" in texts(r)
    c = customer(wa)
    assert (c.full_name, c.pan, c.gender) == ("RAHUL SHARMA", "PFILL2341P", "M")
    req = ReportRequest.objects.get()
    assert req.report_status == ReportStatus.GENERATED and req.payment_links.count() == 1
    assert req.consent.consent_text_version.endswith("-prefill")
    assert ReportRequest.objects.get().consent is not None and req.customer.consents.count() == 1
    assert state(wa) == S.AWAITING_PAYMENT
    assert AuditLog.objects.filter(action="report.prefill", changes__outcome="found").exists()


def test_not_me_discards_the_lookup_and_asks_for_own_details():
    wa = "919800012342"
    say(wa, "hi")
    say(wa, button="agree")
    r = say(wa, button="not_me")
    assert "discarded" in texts(r) and state(wa) == S.ASK_ALT_MOBILE  # maybe a recycled number
    assert r[-1]["buttons"] == ["type_details"]
    assert cache.get(f"prefill-pending:{customer(wa).pk}") is None
    assert customer(wa).pan == ""
    say(wa, button="type_details")
    assert state(wa) == S.ASK_NAME
    say(wa, "Anita Desai")
    say(wa, "ABCDE1234F")
    say(wa, button="female")
    r = say(wa, button="confirm")  # already consented: goes straight to the report, no second consent
    assert "fetching your credit report" in texts(r)
    req = ReportRequest.objects.get()
    assert req.report_status == ReportStatus.GENERATED
    assert customer(wa).pan == "ABCDE1234F" and req.customer.consents.count() == 1


@pytest.mark.parametrize("no_record", [True, False])
def test_falls_back_to_typing_details(no_record):
    wa = "919800012349" if no_record else "919800012348"  # 9 = no record, 8 = provider down
    say(wa, "hi")
    r = say(wa, button="agree")
    if no_record:  # another number is offered first; the customer chooses to type instead
        assert "couldn't find any credit details linked to this WhatsApp number" in texts(r)
        assert state(wa) == S.ASK_ALT_MOBILE
        r = say(wa, button="type_details")
    else:  # provider down: another number wouldn't help
        assert "couldn't look up your details right now" in texts(r)
    assert state(wa) == S.ASK_NAME
    say(wa, "Vishal Rathore")
    say(wa, "EKRPR1234F")
    say(wa, button="male")
    assert "fetching your credit report" in texts(say(wa, button="confirm"))
    assert ReportRequest.objects.get().report_status == ReportStatus.GENERATED


def test_daily_lookup_cap(settings):
    settings.PREFILL_MAX_PER_DAY = 1
    wa = "919800012343"
    say(wa, "hi")
    say(wa, button="agree")
    say(wa, "CANCEL")
    say(wa, "hi")
    r = say(wa, button="agree")
    assert "today's limit" in texts(r) and state(wa) == S.ASK_NAME  # cap reached: asked to type instead
    assert AuditLog.objects.filter(action="report.prefill", changes__outcome="daily_limit").exists()


def test_returning_customer_confirms_saved_details_without_a_new_lookup():
    wa = "919800012344"
    say(wa, "hi")
    say(wa, button="agree")
    say(wa, button="confirm")
    ReportRequest.objects.update(delivery_status="sent", active_lock=None)  # first report finished
    r = say(wa, "hi")
    assert "Welcome back" in texts(r) and state(wa) == S.CONFIRM_DETAILS
    r = say(wa, button="confirm")
    assert "this WhatsApp number" in texts(r) and "look up the details" not in texts(r)
    say(wa, button="agree")
    second = ReportRequest.objects.latest("created_at")
    assert second.consent.consent_text_version.endswith("-direct")
    assert AuditLog.objects.filter(action="report.prefill").count() == 1


def test_decline_and_cancel_clear_the_pending_lookup():
    wa = "919800012345"
    say(wa, "hi")
    say(wa, button="agree")
    assert cache.get(f"prefill-pending:{customer(wa).pk}") is not None
    say(wa, "CANCEL")
    assert cache.get(f"prefill-pending:{customer(wa).pk}") is None


def test_waiting_for_confirmation_is_not_fetched_by_the_cleanup_job():
    with freeze_time("2026-11-24 10:00:00") as clock:
        wa = "919800012346"
        say(wa, "hi")
        say(wa, button="agree")  # consented, looked up, waiting for "Is this you?"
        clock.tick(timedelta(minutes=15))
        assert expire_stale_requests()["fetch_restarted"] == 0
        assert ReportRequest.objects.get().report_status == ReportStatus.CONSENTED
        clock.tick(timedelta(minutes=60))
        Conversation.objects.update(last_inbound_at=timezone.now() - timedelta(hours=2))
        assert expire_stale_requests()["abandoned"] == 1
        assert ReportRequest.objects.get().report_status == ReportStatus.EXPIRED


def test_lookup_details_never_reach_logs_or_message_history(caplog):
    caplog.set_level("DEBUG")
    wa = "919800012347"
    say(wa, "hi")
    say(wa, button="agree")
    from apps.messaging.models import Direction, MessageLog

    inbound = " ".join(
        MessageLog.objects.filter(direction=Direction.INBOUND).values_list("body_redacted", flat=True)
    )
    logged = " ".join(r.getMessage() for r in caplog.records)
    for secret in ("RAHUL", "PFILL2347P"):
        assert secret not in inbound and secret not in logged

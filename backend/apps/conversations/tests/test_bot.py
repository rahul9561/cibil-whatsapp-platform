"""Conversations driven as Meta would deliver them: simulated inbound -> bot -> mock WhatsApp outbox."""

import json
from datetime import date

import pytest
from django.core.cache import cache

from apps.audit.models import AuditLog
from apps.conversations.models import Conversation
from apps.conversations.models import ConversationState as S
from apps.conversations.validators import clean_dob, clean_name, clean_pan
from apps.core.models import WebhookEvent
from apps.customers.models import Customer
from apps.messaging.dev import simulate_inbound
from apps.messaging.models import OTP_PLACEHOLDER, Direction, MessageLog
from apps.payments.gateway.mock import simulate_payment
from apps.payments.tasks import confirm_payment_task
from apps.reports.models import DeliveryStatus, ReportRequest, ReportStatus

pytestmark = pytest.mark.django_db
WA = "919812300001"


def say(text=None, button=None, wa=WA):
    return simulate_inbound(wa, text=text, reply_id=button)


def state(wa=WA):
    return Conversation.objects.get(customer=Customer.objects.by_wa_id(wa).get()).state


def texts(replies):
    return " || ".join(r.get("body", "") for r in replies)


def get_to_otp(wa=WA, pan="ABCDE1234F"):
    say("hi", wa=wa)
    say("Rahul Sharma", wa=wa)
    say(pan, wa=wa)
    say("15-01-1990", wa=wa)
    say(button="confirm", wa=wa)
    return say(button="agree", wa=wa)


# --- happy path ------------------------------------------------------------------------------------------


def test_full_conversation_from_hi_to_delivered_pdf():
    r = say("Hi")
    assert "credit report" in texts(r) and "full name" in texts(r)
    assert state() == S.ASK_NAME

    assert "PAN" in texts(say("Rahul Sharma"))
    assert "date of birth" in texts(say("abcde 1234 f"))
    r = say("15/01/1990")
    assert state() == S.CONFIRM_DETAILS
    assert "Rahul Sharma" in texts(r) and "ABCDE1**4F" in texts(r) and "1990" not in texts(r)
    assert r[-1]["buttons"] == ["confirm", "edit", "reset"]

    r = say(button="confirm")
    assert "Do you agree?" in texts(r) and r[-1]["buttons"] == ["agree", "decline"]
    r = say(button="agree")
    assert state() == S.AWAITING_OTP and "******0001" in texts(r)

    r = say("123456")
    assert "Verified" in texts(r)
    customer = Customer.objects.by_wa_id(WA).get()
    req = ReportRequest.objects.get(customer=customer)
    assert req.report_status == ReportStatus.GENERATED
    assert state() == S.AWAITING_PAYMENT
    link = req.payment_links.get()
    assert req.consent.wa_message_id.startswith("wamid.sim_")

    assert link.short_url in texts(say("how do I pay?"))

    simulate_payment(link.razorpay_link_id)
    confirm_payment_task.delay(link.razorpay_link_id)
    req.refresh_from_db()
    assert req.delivery_status == DeliveryStatus.SENT
    assert state() == S.DELIVERED


def test_otp_and_personal_answers_are_never_stored():
    get_to_otp()
    say("654321")  # wrong
    say("123456")  # right
    for row in MessageLog.objects.filter(direction=Direction.INBOUND).values_list("body_redacted", flat=True):
        for secret in ("654321", "123456", "Rahul Sharma", "ABCDE1234F", "15-01-1990"):
            assert secret not in row
    assert OTP_PLACEHOLDER in MessageLog.objects.values_list("body_redacted", flat=True)
    everything = json.dumps(
        [list(WebhookEvent.objects.values()), list(AuditLog.objects.values())], default=str
    )
    for secret in ("654321", "123456", "ABCDE1234F", "Rahul Sharma"):
        assert secret not in everything
    for wamid in WebhookEvent.objects.values_list("event_id", flat=True):
        assert cache.get("wa:inbound:" + wamid) is None  # parked text is consumed


# --- validation & navigation -----------------------------------------------------------------------------


def test_invalid_answers_are_re_asked():
    say("hi")
    assert "letters only" in texts(say("R2D2"))
    say("Rahul Sharma")
    assert "valid PAN" in texts(say("12345"))
    say("ABCDE1234F")
    assert "18 years" in texts(say(f"01-01-{date.today().year - 10}"))
    assert "18 years" in texts(say("31-02-1990"))
    assert state() == S.ASK_DOB
    say("15-01-1990")
    assert "choose" in texts(say("maybe"))
    say(button="edit")
    assert state() == S.ASK_NAME


def test_returning_customer_skips_to_confirmation():
    get_to_otp()
    say("CANCEL")
    r = say("hi")
    assert "Welcome back" in texts(r) and state() == S.CONFIRM_DETAILS


def test_second_hi_resumes_the_open_request():
    say("hi")
    say("Rahul Sharma")
    assert ReportRequest.objects.count() == 1
    say("CANCEL")
    say("hi")
    assert ReportRequest.objects.count() == 2  # cancelled one + new one, never two open


def test_decline_consent_cancels():
    say("hi")
    say("Rahul Sharma")
    say("ABCDE1234F")
    say("15-01-1990")
    say(button="confirm")
    assert "nothing has been requested" in texts(say(button="decline"))
    assert ReportRequest.objects.get().report_status == ReportStatus.CANCELLED
    assert state() == S.ENDED


# --- OTP paths -------------------------------------------------------------------------------------------


def test_wrong_otps_then_locked_out(settings):
    get_to_otp()
    assert "numeric OTP" in texts(say("one two three"))  # not counted
    for left in range(settings.OTP_MAX_ATTEMPTS - 1, 0, -1):
        assert f"{left} attempt(s) left" in texts(say("999999"))
    assert "Too many incorrect attempts" in texts(say("999999"))
    assert state() == S.REPORT_FAILED
    assert "credit report" in texts(say("hi"))  # can start over


def test_expired_otp_and_resend_limit(settings):
    get_to_otp()
    assert "expired" in texts(say("000000"))
    for _ in range(settings.OTP_MAX_RESENDS):
        assert "new OTP" in texts(say(button="resend"))
    assert "can't send more OTPs" in texts(say(button="resend"))
    assert "Verified" in texts(say("123456"))


def test_no_hit_customer_is_told_and_not_charged():
    get_to_otp(pan="NOHIT0000N")
    r = say("123456")
    assert "no credit history" in texts(r) or "no credit history" in texts(say("status?"))
    req = ReportRequest.objects.get()
    assert req.report_status == ReportStatus.NO_HIT and not req.payment_links.exists()
    assert state() == S.NO_HIT


def test_provider_rejection_at_otp():
    r = get_to_otp(pan="REJEC0000R")
    assert "couldn't process these details" in texts(r)
    assert ReportRequest.objects.get().failure_code == "applicant_rejected"


def test_cancel_not_allowed_after_payment():
    get_to_otp()
    say("123456")
    link = ReportRequest.objects.get().payment_links.get()
    simulate_payment(link.razorpay_link_id)
    from apps.payments.services import PaymentService

    PaymentService().confirm_link_payment(link.razorpay_link_id)
    assert "can't be cancelled" in texts(say("CANCEL"))


# --- commands & media ------------------------------------------------------------------------------------


def test_stop_start_help():
    say("hi")
    assert "won't receive reminders" in texts(say("STOP"))
    assert Customer.objects.by_wa_id(WA).get().is_opted_out
    assert "back on" in texts(say("start"))
    assert "CANCEL" in texts(say("help"))
    assert state() == S.ASK_NAME  # commands don't lose the customer's place


def test_non_text_messages_get_a_polite_reply():
    r = simulate_inbound(WA, msg_type="image")
    assert "only read text" in texts(r)


def test_lost_body_asks_customer_to_resend(monkeypatch):
    monkeypatch.setattr("apps.conversations.bot.take_body", lambda wamid: None)
    assert "send it again" in texts(say("hi"))


# --- validators ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("Rahul Sharma", "Rahul Sharma"),
        ("  A.  K. Singh ", "A. K. Singh"),
        ("राहुल शर्मा", "राहुल शर्मा"),
        ("R", None),
        ("Rahul123", None),
        ("...", None),
    ],
)
def test_clean_name(raw, ok):
    assert clean_name(raw) == ok


@pytest.mark.parametrize(
    ("raw", "ok"),
    [("abcde1234f", "ABCDE1234F"), ("ABCDE 1234 F", "ABCDE1234F"), ("ABCD1234F", None), ("ABCDE12345", None)],
)
def test_clean_pan(raw, ok):
    assert clean_pan(raw) == ok


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("15-01-1990", date(1990, 1, 15)),
        ("15/1/1990", date(1990, 1, 15)),
        ("1990-01-15", date(1990, 1, 15)),
        ("29-02-2001", None),
        ("15-01-2015", None),
        ("15-01-1900", None),
        ("yesterday", None),
    ],
)
def test_clean_dob(raw, ok):
    assert clean_dob(raw, today=date(2026, 11, 23)) == ok


def test_after_delivery_customer_can_start_again():
    get_to_otp()
    say("123456")
    link = ReportRequest.objects.get().payment_links.get()
    simulate_payment(link.razorpay_link_id)
    confirm_payment_task.delay(link.razorpay_link_id)
    assert state() == S.DELIVERED
    r = say("hi")  # no delivery receipt has arrived (mock): must not be stuck on the old request
    assert "Welcome back" in texts(r) and ReportRequest.objects.count() == 2


def test_thanks_after_delivery_does_not_start_a_new_request():
    get_to_otp()
    say("123456")
    link = ReportRequest.objects.get().payment_links.get()
    simulate_payment(link.razorpay_link_id)
    confirm_payment_task.delay(link.razorpay_link_id)
    for polite in ("thank you", "ok", "👍"):
        assert "Send HI whenever" in texts(say(polite))
    assert ReportRequest.objects.count() == 1 and state() == S.DELIVERED
    assert "Welcome back" in texts(say("Hi!"))
    assert ReportRequest.objects.count() == 2


def test_first_contact_starts_on_any_message():
    assert "full name" in texts(say("what is this?"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hi", True),
        ("Hello!", True),
        ("new report please", True),
        ("I want my report", True),
        ("thanks", False),
        ("ok", False),
        ("", False),
    ],
)
def test_wants_new_report(text, expected):
    from apps.conversations.bot import wants_new_report

    assert wants_new_report(text) is expected

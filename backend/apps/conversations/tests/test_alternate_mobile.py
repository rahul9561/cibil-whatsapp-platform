"""Alternate mobile number: offered when the WhatsApp number finds nothing (or someone else's details).

Mock prefill (mock_no_otp) by the number's last digit: 9 = no record, 8 = provider down, else RAHUL SHARMA
with PAN "PFILL" + last 4 digits + "P", male. Details found for a typed number are only used after the
customer types the matching PAN, and nothing from that lookup is shown before.
"""

import pytest

from apps.audit.models import AuditLog
from apps.conversations.models import Conversation
from apps.conversations.models import ConversationState as S
from apps.conversations.validators import clean_mobile
from apps.customers.models import Customer
from apps.messaging.dev import simulate_inbound
from apps.messaging.models import Direction, MessageLog
from apps.reports.models import ReportRequest, ReportStatus
from apps.reports.providers.mock import MockDirectCreditReportProvider

pytestmark = pytest.mark.django_db
OWN_NO_RECORD = "919800022229"  # WhatsApp number with no bureau record
ALT = "98765 40017"  # alternate number with a record: PAN PFILL0017P
ALT_PAN = "PFILL0017P"


@pytest.fixture(autouse=True)
def prefill_provider(settings):
    settings.CREDIT_PROVIDER = "mock_no_otp"
    settings.PREFILL_BY_MOBILE = True
    settings.PREFILL_MAX_PER_DAY = 10


@pytest.fixture
def fetched_mobiles(monkeypatch):
    seen = []
    real = MockDirectCreditReportProvider.fetch_report_without_otp

    def spy(self, applicant, consent_ref):
        seen.append(applicant.mobile)
        return real(self, applicant, consent_ref)

    monkeypatch.setattr(MockDirectCreditReportProvider, "fetch_report_without_otp", spy)
    return seen


def say(wa, text=None, button=None):
    return simulate_inbound(wa, text=text, reply_id=button)


def texts(replies):
    return " || ".join(r.get("body", "") for r in replies)


def state(wa):
    return Conversation.objects.get(customer=Customer.objects.by_wa_id(wa).get()).state


def start(wa=OWN_NO_RECORD):
    say(wa, "hi")
    return say(wa, button="agree")


def test_not_found_offers_another_number_and_the_report_uses_it(fetched_mobiles):
    r = start()
    assert "couldn't find any credit details linked to this WhatsApp number (+91 98******29)" in texts(r)
    assert state(OWN_NO_RECORD) == S.ASK_ALT_MOBILE and r[-1]["buttons"] == ["type_details", "reset"]

    r = say(OWN_NO_RECORD, ALT)
    shown = texts(r)
    assert "We found details linked to +91 98******17" in shown and "type your PAN" in shown
    assert "R***" not in shown and "PF******" not in shown  # nothing revealed before the PAN check
    assert state(OWN_NO_RECORD) == S.VERIFY_ALT_PAN

    r = say(OWN_NO_RECORD, "pfill 0017p")
    shown = texts(r)
    assert (
        "R*** S***" in shown and "PF******7P" in shown and r[-1]["buttons"] == ["confirm", "not_me", "reset"]
    )
    assert Customer.objects.by_wa_id(OWN_NO_RECORD).get().pan == ""  # still not stored until confirmed

    r = say(OWN_NO_RECORD, button="confirm")
    assert "fetching your credit report" in texts(r)
    req = ReportRequest.objects.get()
    assert req.report_status == ReportStatus.GENERATED
    assert req.applicant_mobile == "919876540017" and fetched_mobiles == ["919876540017"]
    c = Customer.objects.by_wa_id(OWN_NO_RECORD).get()
    assert (c.full_name, c.pan) == ("RAHUL SHARMA", ALT_PAN) and c.wa_id == OWN_NO_RECORD
    entry = AuditLog.objects.filter(action="report.alternate_mobile").get()
    assert entry.changes == {"verified_by": "pan_match"} and "40017" not in str(entry.changes)
    assert AuditLog.objects.filter(action="report.prefill", changes__alternate_mobile=True).count() == 1


def test_wrong_pan_discards_the_lookup_without_a_second_try(fetched_mobiles):
    start()
    say(OWN_NO_RECORD, ALT)
    r = say(OWN_NO_RECORD, "ABCDE1234F")
    shown = texts(r)
    assert "doesn't match the details linked to +91 98******17" in shown
    assert "R***" not in shown and state(OWN_NO_RECORD) == S.ASK_NAME
    assert AuditLog.objects.filter(action="report.alternate_mobile_rejected").count() == 1
    say(OWN_NO_RECORD, "Anita Desai")
    say(OWN_NO_RECORD, "ABCDE1234F")
    say(OWN_NO_RECORD, button="female")
    say(OWN_NO_RECORD, button="confirm")
    req = ReportRequest.objects.get()
    assert req.applicant_mobile == "" and fetched_mobiles == [OWN_NO_RECORD]  # own number, typed details


def test_alternate_number_not_found_either_then_tries_run_out():
    start()
    r = say(OWN_NO_RECORD, "9876540019")
    assert "couldn't find any credit details linked to +91 98******19 either" in texts(r)
    assert state(OWN_NO_RECORD) == S.ASK_ALT_MOBILE  # one more try
    r = say(OWN_NO_RECORD, "09876540029")
    assert "either" in texts(r) and "continue with your details" in texts(r)
    assert state(OWN_NO_RECORD) == S.ASK_NAME
    assert AuditLog.objects.filter(action="report.prefill").count() == 3


def test_invalid_own_and_repeated_numbers_are_not_looked_up():
    start()
    r = say(OWN_NO_RECORD, "12345")
    assert "valid Indian mobile number" in texts(r) and state(OWN_NO_RECORD) == S.ASK_ALT_MOBILE
    r = say(OWN_NO_RECORD, "+91 98000 22229")  # their own WhatsApp number again
    assert "already checked" in texts(r)
    say(OWN_NO_RECORD, "9876540019")
    r = say(OWN_NO_RECORD, "98765 40019")
    assert "already checked" in texts(r)
    assert AuditLog.objects.filter(action="report.prefill").count() == 2  # own + one alternate


def test_not_me_offers_another_number():
    wa = "919800022221"  # has a record, but the customer says it's not them (recycled number)
    start(wa)
    r = say(wa, button="not_me")
    assert "discarded" in texts(r) and "different mobile number" in texts(r)
    assert state(wa) == S.ASK_ALT_MOBILE
    say(wa, ALT)
    r = say(wa, ALT_PAN)
    assert "Is this you?" in texts(r)


def test_provider_down_on_the_alternate_number_falls_back_to_typing():
    start()
    r = say(OWN_NO_RECORD, "9876540018")  # 8 = provider down
    assert "couldn't look up your details right now" in texts(r) and state(OWN_NO_RECORD) == S.ASK_NAME


def test_typed_number_and_pan_never_reach_the_message_history():
    start()
    say(OWN_NO_RECORD, ALT)
    say(OWN_NO_RECORD, ALT_PAN)
    inbound = list(
        MessageLog.objects.filter(direction=Direction.INBOUND).values_list("body_redacted", flat=True)
    )
    joined = " ".join(inbound)
    assert "40017" not in joined and ALT_PAN not in joined
    assert "[mobile]" in inbound and "[PAN]" in inbound


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("9876543210", "919876543210"),
        ("98765 43210", "919876543210"),
        ("+91 98765-43210", "919876543210"),
        ("09876543210", "919876543210"),
        ("919876543210", "919876543210"),
        ("5876543210", None),
        ("987654321", None),
        ("abc", None),
        ("", None),
    ],
)
def test_clean_mobile(raw, ok):
    assert clean_mobile(raw) == ok

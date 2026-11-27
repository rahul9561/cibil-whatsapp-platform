"""RESET / "Start over": a stuck customer can start the chat from scratch."""

import pytest

from apps.audit.models import AuditLog
from apps.conversations.models import Conversation
from apps.conversations.models import ConversationState as S
from apps.customers.models import Customer
from apps.messaging.dev import simulate_inbound
from apps.reports.models import ReportRequest, ReportStatus

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def prefill_provider(settings):
    settings.CREDIT_PROVIDER = "mock_no_otp"
    settings.PREFILL_BY_MOBILE = True


def say(wa, text=None, button=None):
    replies = simulate_inbound(wa, text=text, reply_id=button)
    return " || ".join(r.get("body", "") for r in replies), replies


def customer(wa):
    return Customer.objects.by_wa_id(wa).get()


def state(wa):
    return Conversation.objects.get(customer=customer(wa)).state


def test_start_over_button_is_offered_and_resets_everything():
    wa = "919800055501"
    say(wa, "Hi")
    _, replies = say(wa, button="agree")  # "Is this you?" with the looked-up details
    assert replies[-1]["buttons"] == ["confirm", "not_me", "reset"]

    text, replies = say(wa, button="reset")
    assert "chat has been reset" in text and replies[-1]["buttons"] == ["hi"]
    req = ReportRequest.objects.get()
    assert req.report_status == ReportStatus.CANCELLED and req.active_lock is None
    conv = Conversation.objects.get(customer=customer(wa))
    assert conv.state == S.NEW and conv.active_report_request is None and conv.context == {}
    assert AuditLog.objects.filter(action="conversation.reset").count() == 1

    text, _ = say(wa, button="hi")  # Start again -> fresh request, prefill runs again
    assert "Hello!" in text and state(wa) == S.AWAITING_CONSENT
    assert ReportRequest.objects.count() == 2


@pytest.mark.parametrize("typed", ["RESET", "reset chat", "Start over", "restart"])
def test_reset_can_be_typed(typed):
    wa = f"9198000555{10 + len(typed)}"
    say(wa, "Hi")
    text, _ = say(wa, typed)
    assert "chat has been reset" in text and state(wa) == S.NEW


def test_reset_forgets_typed_details_so_prefill_runs_again(settings):
    settings.PREFILL_BY_MOBILE = False
    wa = "919800055502"
    say(wa, "Hi")
    say(wa, "Anita Desai")
    say(wa, "ABCDE1234F")
    assert customer(wa).pan == "ABCDE1234F"
    say(wa, "RESET")
    c = customer(wa)
    assert (c.full_name, c.pan, c.pan_hash, c.gender) == ("", "", "", "")
    settings.PREFILL_BY_MOBILE = True
    text, _ = say(wa, "Hi")
    assert "Welcome back" not in text and state(wa) == S.AWAITING_CONSENT  # not stuck on old details


def test_a_report_being_paid_for_is_never_thrown_away(settings):
    settings.CREDIT_PROVIDER = "mock_no_otp"
    wa = "919800055503"
    say(wa, "Hi")
    say(wa, button="agree")
    say(wa, button="confirm")  # report generated, payment link sent
    req = ReportRequest.objects.get()
    assert req.report_status == ReportStatus.GENERATED
    text, _ = say(wa, "RESET")
    assert "can't be cancelled" in text
    req.refresh_from_db()
    assert req.report_status == ReportStatus.GENERATED and customer(wa).pan  # untouched


def test_reset_keeps_the_language(settings):
    settings.WHATSAPP_ASK_LANGUAGE = True
    wa = "919800055504"
    say(wa, "Hi")
    say(wa, button="lang_hi")
    text, replies = say(wa, "RESET")
    assert "चैट रीसेट" in text and replies[-1]["buttons"] == ["hi"]
    assert customer(wa).language == "hi"

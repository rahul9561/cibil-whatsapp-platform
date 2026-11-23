"""End-to-end report pipeline against mock adapters: tasks run eagerly, exactly as a worker would run them."""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.service import verify_chain
from apps.conversations.models import Conversation
from apps.core.integrations import PermanentError
from apps.core.testing.factories import CONSENT_TEXT, CustomerFactory
from apps.followups.models import FollowUp, FollowUpStatus, ReminderStatus
from apps.messaging.gateway.mock import outbox
from apps.messaging.models import MessageLog
from apps.payments.gateway.mock import simulate_payment
from apps.payments.models import PaymentLinkStatus, PaymentStatus
from apps.payments.services import PaymentMismatch
from apps.payments.tasks import confirm_payment_task
from apps.reports.models import DeliveryStatus, InvalidTransition, ReportRequest, ReportStatus
from apps.reports.providers.base import VerifyOutcome
from apps.reports.providers.mock import MockCreditReportProvider
from apps.reports.services import NotPaid, OtpResendLimit, ReportService
from apps.reports.tasks import deliver_report_task, fetch_report_task, initiate_otp_task

pytestmark = pytest.mark.django_db


def customer_with(pan="ABCDE1234F", in_window=True, wa_id="919812345678"):
    c = CustomerFactory(wa_id=wa_id, pan=pan)
    Conversation.objects.create(customer=c, last_inbound_at=timezone.now() if in_window else None)
    return c


def verified_request(customer) -> ReportRequest:
    svc = ReportService()
    req, created = svc.start_request(customer)
    assert created
    svc.record_consent(req.pk, text_version="v1", text=CONSENT_TEXT, wa_message_id="wamid.consent")
    assert initiate_otp_task.delay(str(req.pk)).get() == "otp_sent"
    assert svc.verify_otp(req.pk, "123456") == VerifyOutcome.VERIFIED
    req.refresh_from_db()
    assert req.report_status == ReportStatus.OTP_VERIFIED
    return req


@pytest.fixture
def eager_retries():
    """Celery re-runs eager retries in-process only when eager exceptions don't propagate; each retry
    re-reads the global setting, so it has to be switched off for the whole run."""
    from config.celery import app

    previous = app.conf.task_eager_propagates
    app.conf.task_eager_propagates = False
    yield
    app.conf.task_eager_propagates = previous


def run_retrying(task, req):
    return task.apply(args=[str(req.pk)]).get(propagate=False)


def actions() -> list[str]:
    return list(AuditLog.objects.order_by("id").values_list("action", flat=True))


# --- happy path ---------------------------------------------------------------------------------------


def test_full_happy_path_from_consent_to_delivery(settings):
    customer = customer_with()
    req = verified_request(customer)

    assert fetch_report_task.delay(str(req.pk)).get() == ReportStatus.GENERATED
    req.refresh_from_db()
    assert req.report_status == ReportStatus.GENERATED
    assert req.expires_at > timezone.now()

    # PDF stored encrypted in private storage, never in plaintext.
    doc = req.document
    raw = (settings.PRIVATE_STORAGE_ROOT / doc.storage_key[:2] / doc.storage_key).read_bytes()
    assert b"%PDF" not in raw and b"MOCK CREDIT REPORT" not in raw

    # Payment link created and sent; follow-up opened with the reminder schedule.
    link = req.payment_links.get()
    assert link.status == PaymentLinkStatus.ACTIVE and link.amount_paise == 9900 and link.sent_count == 1
    sent = outbox()
    assert sent[-1]["kind"] == "text" and link.short_url in sent[-1]["body"] and "₹99" in sent[-1]["body"]
    followup = FollowUp.objects.get(report_request=req)
    assert followup.status == FollowUpStatus.OPEN
    assert followup.reminders.filter(status=ReminderStatus.SCHEDULED).count() == 3

    # Customer pays; confirmation verifies with the gateway, closes the follow-up and delivers.
    simulate_payment(link.razorpay_link_id)
    assert confirm_payment_task.delay(link.razorpay_link_id).get() == "paid"

    link.refresh_from_db()
    assert link.status == PaymentLinkStatus.PAID
    payment = link.payments.get()
    assert payment.status == PaymentStatus.CAPTURED and payment.amount_paise == 9900
    followup.refresh_from_db()
    assert followup.status == FollowUpStatus.CLOSED_PAID and followup.closed_at is not None
    assert not followup.reminders.filter(status=ReminderStatus.SCHEDULED).exists()

    req.refresh_from_db()
    assert req.delivery_status == DeliveryStatus.SENT
    docs = [m for m in outbox() if m["kind"] == "document"]
    assert len(docs) == 1 and docs[0]["filename"] == f"credit-report-{req.public_ref}.pdf"

    # Replays change nothing: no second payment row, no second document.
    assert confirm_payment_task.delay(link.razorpay_link_id).get() == "paid"
    deliver_report_task.delay(str(req.pk)).get()
    assert link.payments.count() == 1
    assert len([m for m in outbox() if m["kind"] == "document"]) == 1

    for expected in [
        "report.started",
        "report.consented",
        "report.otp_sent",
        "report.otp_checked",
        "report.generated",
        "payment_link.created",
        "followup.opened",
        "payment.captured",
        "followup.closed",
        "report.delivered",
    ]:
        assert expected in actions(), expected
    assert verify_chain()[0]


def test_otp_is_never_persisted():
    req = verified_request(customer_with())
    fetch_report_task.delay(str(req.pk)).get()
    for model in (AuditLog, MessageLog, ReportRequest):
        for row in model.objects.all().values():
            assert "123456" not in str(row), model


# --- duplicates & idempotency -------------------------------------------------------------------------


def test_second_start_returns_the_open_request():
    customer = customer_with()
    svc = ReportService()
    first, created = svc.start_request(customer)
    again, created_again = svc.start_request(customer)
    assert created and not created_again and again.pk == first.pk


def test_generated_unpaid_report_is_reused_not_refetched():
    customer = customer_with()
    req = verified_request(customer)
    fetch_report_task.delay(str(req.pk)).get()
    with mock.patch.object(MockCreditReportProvider, "fetch_report") as fetch:
        assert ReportService().fetch_report(req.pk) == ReportStatus.GENERATED
        fetch.assert_not_called()
    again, created = ReportService().start_request(customer)
    assert not created and again.pk == req.pk


def test_fetch_retries_transient_errors_then_succeeds_once(eager_retries):
    req = verified_request(customer_with(pan="FLAKY0000F"))
    with mock.patch.object(
        MockCreditReportProvider,
        "fetch_report",
        autospec=True,
        side_effect=MockCreditReportProvider.fetch_report,
    ) as fetch:
        assert run_retrying(fetch_report_task, req) == ReportStatus.GENERATED
    assert fetch.call_count == 3  # two timeouts, then success
    req.refresh_from_db()
    assert req.report_status == ReportStatus.GENERATED
    assert req.payment_links.count() == 1


def test_fetch_gives_up_after_max_retries_and_marks_failed(eager_retries):
    req = verified_request(customer_with(pan="DOWNS0000D"))
    run_retrying(fetch_report_task, req)
    req.refresh_from_db()
    assert req.report_status == ReportStatus.FAILED and req.failure_code == "provider_unavailable"
    assert not req.payment_links.exists()
    assert req.active_lock is None  # customer can try again later


# --- OTP failure paths ---------------------------------------------------------------------------------


def _otp_sent_request(customer=None):
    customer = customer or customer_with()
    svc = ReportService()
    req, _ = svc.start_request(customer)
    svc.record_consent(req.pk, text_version="v1", text=CONSENT_TEXT)
    initiate_otp_task.delay(str(req.pk)).get()
    return svc, req


def test_wrong_otp_attempts_exhausted_fails_request(settings):
    svc, req = _otp_sent_request()
    assert svc.verify_otp(req.pk, "hello") == VerifyOutcome.INVALID  # not an OTP: not counted
    for _ in range(settings.OTP_MAX_ATTEMPTS):
        assert svc.verify_otp(req.pk, "999999") == VerifyOutcome.INVALID
    req.refresh_from_db()
    assert req.report_status == ReportStatus.FAILED and req.failure_code == "otp_attempts_exceeded"
    assert req.otp_attempts == settings.OTP_MAX_ATTEMPTS
    with pytest.raises(InvalidTransition):
        svc.verify_otp(req.pk, "123456")
    _, created = svc.start_request(req.customer)
    assert created


def test_expired_otp_then_resend_until_limit(settings):
    svc, req = _otp_sent_request()
    assert svc.verify_otp(req.pk, "000000") == VerifyOutcome.EXPIRED
    req.refresh_from_db()
    req.otp_expires_at = timezone.now() - timedelta(seconds=1)
    req.save(update_fields=["otp_expires_at"])
    assert svc.verify_otp(req.pk, "123456") == VerifyOutcome.EXPIRED  # our own expiry check, no provider call
    for _ in range(settings.OTP_MAX_RESENDS):
        svc.initiate_otp(req.pk)
    with pytest.raises(OtpResendLimit):
        svc.initiate_otp(req.pk)
    assert svc.verify_otp(req.pk, "123456") == VerifyOutcome.VERIFIED


def test_provider_rejection_on_otp_marks_failed():
    customer = customer_with(pan="REJEC0000R")
    svc = ReportService()
    req, _ = svc.start_request(customer)
    svc.record_consent(req.pk, text_version="v1", text=CONSENT_TEXT)
    initiate_otp_task.delay(str(req.pk)).get()
    req.refresh_from_db()
    assert req.report_status == ReportStatus.FAILED and req.failure_code == "applicant_rejected"


def test_no_hit_creates_no_payment_link():
    req = verified_request(customer_with(pan="NOHIT0000N"))
    assert fetch_report_task.delay(str(req.pk)).get() == ReportStatus.NO_HIT
    req.refresh_from_db()
    assert req.report_status == ReportStatus.NO_HIT
    assert not req.payment_links.exists() and not FollowUp.objects.exists()


# --- payment & delivery failure paths ------------------------------------------------------------------


def _generated(customer=None):
    req = verified_request(customer or customer_with())
    fetch_report_task.delay(str(req.pk)).get()
    req.refresh_from_db()
    return req, req.payment_links.get()


def test_amount_mismatch_is_rejected_and_flagged():
    req, link = _generated()
    simulate_payment(link.razorpay_link_id, amount_paise=link.amount_paise + 100)
    with pytest.raises(PaymentMismatch):
        confirm_payment_task.delay(link.razorpay_link_id).get()
    link.refresh_from_db()
    assert link.status == PaymentLinkStatus.ACTIVE and not link.payments.exists()
    assert "payment.mismatch" in actions()
    req.refresh_from_db()
    assert req.delivery_status == DeliveryStatus.NOT_DELIVERED


def test_partial_payment_does_not_confirm():
    _, link = _generated()
    simulate_payment(link.razorpay_link_id, amount_paise=100)
    assert confirm_payment_task.delay(link.razorpay_link_id).get() == "not_paid"


def test_delivery_requires_verified_payment():
    req, _ = _generated()
    with pytest.raises(NotPaid):
        ReportService().deliver(req.pk)


def test_delivery_outside_window_uses_document_template(settings):
    req, link = _generated()
    Conversation.objects.filter(customer=req.customer).update(
        last_inbound_at=timezone.now() - timedelta(days=2)
    )
    simulate_payment(link.razorpay_link_id)
    confirm_payment_task.delay(link.razorpay_link_id).get()
    last = outbox()[-1]
    assert last["kind"] == "template"
    assert last["template"] == settings.WHATSAPP_TEMPLATES["report_delivery"]
    assert last["media_id"] and last["filename"].endswith(".pdf")


def test_permanent_delivery_failure_is_recorded():
    req, link = _generated()
    simulate_payment(link.razorpay_link_id)
    from apps.messaging.gateway.mock import MockWhatsAppGateway

    MockWhatsAppGateway.fail_next = PermanentError("blocked", code="recipient_blocked")
    with pytest.raises(PermanentError):
        confirm_payment_task.delay(link.razorpay_link_id).get()
    req.refresh_from_db()
    assert req.delivery_status == DeliveryStatus.FAILED
    assert MessageLog.objects.get(idempotency_key=f"deliver:{req.pk}").error_code == "recipient_blocked"

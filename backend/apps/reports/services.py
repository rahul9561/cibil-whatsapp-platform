"""Report request lifecycle: consent -> OTP -> verify -> fetch -> store -> deliver.

See ARCHITECTURE §4, §6.2, §6.5.

Concurrency model: the DB row lock (select_for_update) guards status changes; a short distributed lock
guards each external call so two workers never call the provider for the same request at once.
The provider is never called while a DB row lock is held.
"""

import contextlib
import hashlib
import re
from datetime import date, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import service as audit
from apps.audit.models import ActorType
from apps.conversations.i18n import copy_for
from apps.core.integrations import NotConfiguredError, PermanentError, distributed_lock
from apps.customers.models import ConsentRecord
from apps.messaging.services import MessagingService
from apps.payments.models import PaymentLinkStatus

from .models import DeliveryStatus, InvalidTransition, ReportDocument, ReportRequest, ReportStatus
from .providers import get_prefill_provider, get_provider
from .providers.base import Applicant, OtpSession, Prefill, ReportOutcome, VerifyOutcome
from .storage import ReportStorage

_OTP_FORMAT = re.compile(r"^\d{4,8}$")


class OtpResendLimit(PermanentError):
    def __init__(self):
        super().__init__("OTP resend limit reached", code="otp_resend_limit")


class NotPaid(PermanentError):
    def __init__(self):
        super().__init__("report has no verified payment", code="not_paid")


class PrefillLimitReached(PermanentError):
    def __init__(self):
        super().__init__("daily prefill lookups used up", code="prefill_daily_limit")


def _system(action, obj, **changes):
    audit.record(action, actor_type=ActorType.SYSTEM, obj=obj, changes=changes)


class ReportService:
    def __init__(
        self, provider=None, storage: ReportStorage | None = None, messaging: MessagingService | None = None
    ):
        self._provider = provider
        self.storage = storage or ReportStorage()
        self._messaging = messaging

    @property
    def provider(self):
        if self._provider is None:
            self._provider = get_provider()
        return self._provider

    @property
    def messaging(self) -> MessagingService:
        if self._messaging is None:
            self._messaging = MessagingService()
        return self._messaging

    # -- creation & consent --------------------------------------------------------------------------

    def start_request(self, customer) -> tuple[ReportRequest, bool]:
        """Return (request, created). An existing open request is returned instead of a duplicate."""
        if settings.REPORT_PRICE_PAISE <= 0:
            raise NotConfiguredError("REPORT_PRICE_PAISE is not set", code="price_not_configured")
        existing = ReportRequest.objects.filter(active_lock=customer.pk).first()
        if existing:
            return existing, False
        try:
            with transaction.atomic():
                req = ReportRequest.objects.create(customer=customer, price_paise=settings.REPORT_PRICE_PAISE)
        except IntegrityError:  # lost a race with a concurrent start for the same customer
            return ReportRequest.objects.get(active_lock=customer.pk), False
        _system("report.started", req)
        return req, True

    def prefill(self, request_id, mobile: str | None = None) -> Prefill | None:
        """Look up name/PAN/gender for a mobile number (only after consent).

        `mobile` defaults to the customer's WhatsApp number; an alternate number is one the customer typed.
        Returns None when the provider has no prefill or nothing clear was found. Raises PrefillLimitReached
        past PREFILL_MAX_PER_DAY (all numbers count). TransientError propagates so the caller can fall back
        to asking the customer.
        """
        req = ReportRequest.objects.select_related("customer").get(pk=request_id)
        if not req.consent_id:
            raise InvalidTransition(f"{req.public_ref}: consent is required before a bureau lookup")
        prefill_provider = get_prefill_provider()
        if prefill_provider is None:
            return None
        key = f"prefill-count:{req.customer_id}"
        cache.add(key, 0, timeout=24 * 60 * 60)
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=24 * 60 * 60)
            count = 1
        if count > settings.PREFILL_MAX_PER_DAY:
            _system("report.prefill", req, outcome="daily_limit")
            raise PrefillLimitReached()
        alternate = bool(mobile) and mobile != req.customer.wa_id
        try:
            result = prefill_provider.prefill_by_mobile(mobile or req.customer.wa_id, str(req.consent_id))
        except Exception as exc:
            # Only answered lookups use up the daily allowance: a refused call (bad token, provider down)
            # must not lock the customer out for the day (2026-11-26: three 401s did exactly that).
            with contextlib.suppress(ValueError):  # counter already expired
                cache.decr(key)
            _system(
                "report.prefill",
                req,
                outcome="error",
                code=getattr(exc, "code", type(exc).__name__),
                alternate_mobile=alternate,
                prefill_provider=prefill_provider.name,
            )
            raise
        _system(
            "report.prefill",
            req,
            outcome="found" if result else "not_found",
            pan_found=bool(result and result.pan),
            alternate_mobile=alternate,
            prefill_provider=prefill_provider.name,
        )
        return result

    def use_alternate_mobile(self, request_id, mobile: str) -> None:
        """The customer proved an alternate number (matching PAN): the bureau gets that number."""
        with transaction.atomic():
            req = ReportRequest.objects.select_for_update().get(pk=request_id)
            if req.report_status != ReportStatus.CONSENTED:
                raise InvalidTransition(f"{req.public_ref}: mobile can't change in {req.report_status}")
            req.applicant_mobile = mobile
            req.save(update_fields=["applicant_mobile", "updated_at"])
            _system("report.alternate_mobile", req, verified_by="pan_match")  # the number itself: encrypted

    def record_consent(
        self, request_id, *, text_version: str, text: str, wa_message_id: str = ""
    ) -> ConsentRecord:
        with transaction.atomic():
            req = ReportRequest.objects.select_for_update().select_related("customer").get(pk=request_id)
            if req.consent_id:
                return req.consent
            req.transition_to(ReportStatus.CONSENTED)
            consent = ConsentRecord.objects.create(
                customer=req.customer,
                purpose="credit_report_self",
                consent_text_version=text_version,
                consent_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                wa_message_id=wa_message_id,
            )
            req.consent = consent
            req.save()
            _system("report.consented", req, consent_version=text_version)
        return consent

    # -- OTP -----------------------------------------------------------------------------------------

    @staticmethod
    def _applicant(customer, mobile: str = "") -> Applicant:
        return Applicant(
            full_name=customer.full_name,
            pan=customer.pan,
            dob=date.fromisoformat(customer.dob) if customer.dob else None,
            mobile=mobile or customer.wa_id,
            email=customer.email,
            pincode=customer.pincode,
            gender=customer.gender,
        )

    def initiate_otp(self, request_id) -> OtpSession:
        """First call sends the OTP; later calls (while OTP_SENT) resend, up to OTP_MAX_RESENDS."""
        with distributed_lock(f"report-otp:{request_id}"):
            req = ReportRequest.objects.select_related("customer").get(pk=request_id)
            if req.report_status == ReportStatus.CONSENTED:
                session = self.provider.initiate_otp(
                    self._applicant(req.customer), consent_ref=str(req.consent_id)
                )
                resend = False
            elif req.report_status == ReportStatus.OTP_SENT:
                if req.otp_resends >= settings.OTP_MAX_RESENDS:
                    raise OtpResendLimit()
                session = self.provider.resend_otp(req.provider_session_ref)
                resend = True
            else:
                raise InvalidTransition(f"{req.public_ref}: cannot send OTP from {req.report_status}")

            with transaction.atomic():
                req = ReportRequest.objects.select_for_update().get(pk=request_id)
                req.transition_to(ReportStatus.OTP_SENT)
                req.provider_name = self.provider.name
                req.provider_session_ref = session.session_ref
                req.otp_sent_at, req.otp_expires_at = timezone.now(), session.expires_at
                req.otp_attempts = 0  # a fresh OTP gets fresh attempts; resends are capped separately
                if resend:
                    req.otp_resends += 1
                req.save()
                _system("report.otp_resent" if resend else "report.otp_sent", req)
        return session

    def verify_otp(self, request_id, otp: str) -> VerifyOutcome:
        """Verify via the provider. The OTP is only ever held in memory here (ARCHITECTURE §6.5)."""
        otp = (otp or "").strip()
        with distributed_lock(f"report-otp:{request_id}"):
            with transaction.atomic():
                req = ReportRequest.objects.select_for_update().get(pk=request_id)
                if req.report_status != ReportStatus.OTP_SENT:
                    raise InvalidTransition(f"{req.public_ref}: no OTP pending ({req.report_status})")
                if not _OTP_FORMAT.match(otp):
                    return VerifyOutcome.INVALID  # typo / not an OTP: not counted as an attempt
                if req.otp_expires_at and req.otp_expires_at <= timezone.now():
                    return VerifyOutcome.EXPIRED
                # Count the attempt *before* calling the provider so a crash can't grant free guesses.
                req.otp_attempts += 1
                req.save(update_fields=["otp_attempts"])
                session_ref = req.provider_session_ref

            outcome = self.provider.verify_otp(session_ref, otp)
            del otp

            with transaction.atomic():
                req = ReportRequest.objects.select_for_update().get(pk=request_id)
                if outcome == VerifyOutcome.VERIFIED:
                    req.transition_to(ReportStatus.OTP_VERIFIED)
                elif outcome == VerifyOutcome.LOCKED:
                    req.transition_to(ReportStatus.FAILED, failure_code="otp_locked")
                elif outcome == VerifyOutcome.INVALID and req.otp_attempts >= settings.OTP_MAX_ATTEMPTS:
                    req.transition_to(ReportStatus.FAILED, failure_code="otp_attempts_exceeded")
                req.save()
                _system("report.otp_checked", req, outcome=str(outcome), attempt=req.otp_attempts)
        return outcome

    # -- fetch ---------------------------------------------------------------------------------------

    def fetch_report(self, request_id) -> str:
        """Fetch exactly once. Returns the resulting report status. TransientError propagates for retry."""
        with distributed_lock(f"report-fetch:{request_id}", timeout=300):
            with transaction.atomic():
                req = ReportRequest.objects.select_for_update().get(pk=request_id)
                if req.report_status in {ReportStatus.GENERATED, ReportStatus.NO_HIT, ReportStatus.FAILED}:
                    return req.report_status  # already done: idempotent
                direct = not self.provider.requires_otp
                claimable = ReportStatus.CONSENTED if direct else ReportStatus.OTP_VERIFIED
                if req.report_status == claimable:
                    blocked = self._pan_guard(req) if direct else ""
                    if blocked:
                        req.transition_to(ReportStatus.FAILED, failure_code=blocked)
                        req.save()
                        _system("report.blocked", req, code=blocked)
                        return ReportStatus.FAILED
                    req.transition_to(ReportStatus.FETCHING)  # claim: no second fetch can start
                    req.provider_name = self.provider.name
                    req.pan_hash = req.customer.pan_hash
                    req.save()
                elif req.report_status != ReportStatus.FETCHING:  # FETCHING = resume after crash/retry
                    raise InvalidTransition(f"{req.public_ref}: cannot fetch from {req.report_status}")
                session_ref, consent_id, customer = req.provider_session_ref, req.consent_id, req.customer
                mobile = req.applicant_mobile

            try:
                if direct:
                    if not consent_id:
                        return self.mark_failed(request_id, "consent_missing")
                    result = self.provider.fetch_report_without_otp(
                        self._applicant(customer, mobile), str(consent_id)
                    )
                else:
                    result = self.provider.fetch_report(session_ref)
            except PermanentError as exc:
                return self.mark_failed(request_id, exc.code)

            if result.outcome == ReportOutcome.NO_HIT:
                with transaction.atomic():
                    req = ReportRequest.objects.select_for_update().get(pk=request_id)
                    req.transition_to(ReportStatus.NO_HIT)
                    req.save()
                    _system("report.no_hit", req)
                return ReportStatus.NO_HIT

            if not result.pdf_bytes.startswith(b"%PDF-"):
                return self.mark_failed(request_id, "invalid_report_document")

            stored = self.storage.save(result.pdf_bytes)
            now = timezone.now()
            with transaction.atomic():
                req = ReportRequest.objects.select_for_update().get(pk=request_id)
                req.transition_to(ReportStatus.GENERATED)
                req.provider_report_ref = result.report_ref
                req.expires_at = now + timedelta(days=settings.REPORT_VALIDITY_DAYS)
                req.save()
                ReportDocument.objects.create(
                    report_request=req,
                    storage_key=stored.storage_key,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    encryption_key_id=stored.key_id,
                    score=result.score,
                    purge_after=now + timedelta(days=settings.REPORT_PDF_RETENTION_DAYS),
                )
                _system("report.generated", req, size_bytes=stored.size_bytes)
        return ReportStatus.GENERATED

    @staticmethod
    def _pan_guard(req: ReportRequest) -> str:
        """Misuse guard for providers that don't verify PAN ownership (no OTP). Returns a failure code or "".

        Blocks, within PAN_GUARD_DAYS: (a) a PAN already reported for a *different* WhatsApp number, and
        (b) a WhatsApp number that already got a report for a *different* PAN. Stops one number from
        pulling many people's reports, and stops a PAN being pulled from several numbers.
        """
        days = settings.PAN_GUARD_DAYS
        pan_hash = req.customer.pan_hash
        if days <= 0 or not pan_hash:
            return ""
        since = timezone.now() - timedelta(days=days)
        recent = ReportRequest.objects.filter(generated_at__gte=since).exclude(pk=req.pk)
        if recent.filter(pan_hash=pan_hash).exclude(customer_id=req.customer_id).exists():
            return "pan_used_by_other_number"
        if (
            recent.filter(customer_id=req.customer_id)
            .exclude(pan_hash=pan_hash)
            .exclude(pan_hash="")
            .exists()
        ):
            return "number_used_for_other_pan"
        return ""

    def mark_failed(self, request_id, code: str, detail: str = "") -> str:
        with transaction.atomic():
            req = ReportRequest.objects.select_for_update().get(pk=request_id)
            if req.can_transition_to(ReportStatus.FAILED):
                req.transition_to(ReportStatus.FAILED, failure_code=code, failure_detail=detail)
                req.save()
                _system("report.failed", req, code=code)
            return req.report_status

    def cancel(self, request_id, *, by_user, reason: str) -> ReportRequest:
        """Staff/customer cancellation: also cancels open payment links and closes the follow-up."""
        return self.close(request_id, by_user=by_user, reason=reason, final_status=ReportStatus.CANCELLED)

    def expire(self, request_id, *, reason: str) -> ReportRequest:
        """Timed out (abandoned, or generated but never paid). Same tidy-up as a cancellation."""
        return self.close(request_id, by_user=None, reason=reason, final_status=ReportStatus.EXPIRED)

    def close(self, request_id, *, by_user, reason: str, final_status: str) -> ReportRequest:
        from apps.followups.models import FollowUpStatus, ReminderStatus
        from apps.payments.gateway import get_payment_gateway

        with transaction.atomic():
            req = ReportRequest.objects.select_for_update().get(pk=request_id)
            if req.payment_links.filter(status=PaymentLinkStatus.PAID).exists():
                raise InvalidTransition(f"{req.public_ref}: already paid; refund instead of cancelling")
            req.transition_to(final_status, failure_code=f"{final_status}", failure_detail=reason)
            req.save()
            open_links = list(
                req.payment_links.filter(status__in=[PaymentLinkStatus.PENDING, PaymentLinkStatus.ACTIVE])
            )
            for link in open_links:
                link.status = PaymentLinkStatus.CANCELLED
                link.save(update_fields=["status"])
            followup = getattr(req, "followup", None)
            if followup is not None and followup.is_open:
                lost = final_status == ReportStatus.EXPIRED
                followup.status = FollowUpStatus.CLOSED_LOST if lost else FollowUpStatus.CLOSED_MANUAL
                followup.closed_at = timezone.now()
                followup.close_reason, followup.next_reminder_at = (
                    f"Request {final_status}: {reason}"[:255],
                    None,
                )
                followup.save()
                followup.reminders.filter(status=ReminderStatus.SCHEDULED).update(
                    status=ReminderStatus.CANCELLED, skip_reason=f"request_{final_status}"
                )
            audit.record(
                f"report.{final_status}",
                actor=by_user,
                actor_type=None if by_user else "system",
                obj=req,
                changes={"reason": reason},
            )
        gateway = get_payment_gateway()
        for link in open_links:
            if link.razorpay_link_id:
                gateway.cancel_payment_link(link.razorpay_link_id)
        return req

    # -- delivery ------------------------------------------------------------------------------------

    def deliver(self, request_id):
        """Send the PDF after a verified payment. Idempotent: delivers at most once."""
        with transaction.atomic():
            req = (
                ReportRequest.objects.select_for_update()
                .select_related("customer", "document")
                .get(pk=request_id)
            )
            if req.delivery_status in {DeliveryStatus.SENT, DeliveryStatus.DELIVERED, DeliveryStatus.READ}:
                return None
            if req.report_status != ReportStatus.GENERATED:
                raise InvalidTransition(f"{req.public_ref}: cannot deliver from {req.report_status}")
            if not req.payment_links.filter(status=PaymentLinkStatus.PAID).exists():
                raise NotPaid()
            req.delivery_status = DeliveryStatus.QUEUED
            req.save(update_fields=["delivery_status"])
            doc = req.document

        content = self.storage.read(doc.storage_key, expected_sha256=doc.sha256)
        try:
            msg = self.messaging.send_document(
                req.customer,
                content=content,
                filename=f"credit-report-{req.public_ref}.pdf",
                caption=copy_for(req.customer.language).DELIVERY_CAPTION.format(ref=req.public_ref),
                template_params=[req.public_ref],
                idempotency_key=f"deliver:{req.pk}",
                related_request=req,
            )
        except PermanentError as exc:
            ReportRequest.objects.filter(pk=req.pk).update(delivery_status=DeliveryStatus.FAILED)
            _system("report.delivery_failed", req, code=exc.code)
            raise

        with transaction.atomic():
            req = ReportRequest.objects.select_for_update().get(pk=request_id)
            req.delivery_status = DeliveryStatus.SENT
            req.save(update_fields=["delivery_status"])
            _system("report.delivered", req, message=str(msg.pk))
        return msg

"""Payment links and payment confirmation (ARCHITECTURE §6.1).

Rules:
- A link is created only for a GENERATED report, reusing the open link when it is still valid.
- A payment is accepted only after re-fetching the link from the gateway and checking reference, status,
  currency and amount. A webhook body on its own is never trusted.
- Everything is idempotent: confirming twice, or re-running after a crash, changes nothing.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.audit import service as audit
from apps.audit.models import ActorType
from apps.core.integrations import PermanentError
from apps.messaging.services import MessagingService
from apps.reports.models import ReportRequest, ReportStatus

from .gateway import get_payment_gateway
from .models import Payment, PaymentLink, PaymentLinkStatus, PaymentStatus

# Don't hand a customer a link that dies before they can reasonably pay.
MIN_REMAINING_VALIDITY = timedelta(hours=1)


class NotPayable(PermanentError):
    def __init__(self, reason: str):
        super().__init__(f"report request is not payable: {reason}", code=f"not_payable_{reason}")


class LinkLookupFailed(PermanentError):
    """The gateway refused to tell us about a link (unknown link, wrong keys). Keeps the original code."""

    def __init__(self, cause: PermanentError):
        super().__init__(f"payment link lookup failed: {cause}", code=cause.code)


class PaymentMismatch(PermanentError):
    def __init__(self, reason: str):
        super().__init__(f"gateway payment does not match our link: {reason}", code="payment_mismatch")


def format_rupees(paise: int) -> str:
    return f"₹{paise // 100}" if paise % 100 == 0 else f"₹{paise / 100:.2f}"


class PaymentService:
    def __init__(self, gateway=None, messaging: MessagingService | None = None):
        self.gw = gateway or get_payment_gateway()
        self._messaging = messaging

    @property
    def messaging(self) -> MessagingService:
        if self._messaging is None:
            self._messaging = MessagingService()
        return self._messaging

    # -- links -------------------------------------------------------------------------------------

    def get_or_create_link(self, report_request_id, *, created_by=None) -> PaymentLink:
        to_cancel = None
        with transaction.atomic():
            req = ReportRequest.objects.select_for_update().get(pk=report_request_id)
            if req.report_status != ReportStatus.GENERATED:
                raise NotPayable(req.report_status)
            if req.payment_links.filter(status=PaymentLinkStatus.PAID).exists():
                raise NotPayable("already_paid")
            link = req.payment_links.filter(
                status__in=[PaymentLinkStatus.PENDING, PaymentLinkStatus.ACTIVE]
            ).first()
            now = timezone.now()
            if link and link.status == PaymentLinkStatus.ACTIVE:
                if link.expires_at is None or link.expires_at - now > MIN_REMAINING_VALIDITY:
                    return link  # reuse the valid link
                link.status = PaymentLinkStatus.CANCELLED  # about to expire: replace it
                link.save(update_fields=["status"])
                to_cancel, link = link.razorpay_link_id, None
            if link is None:
                attempt = (req.payment_links.aggregate(m=Max("attempt"))["m"] or 0) + 1
                link = PaymentLink.objects.create(
                    report_request=req,
                    attempt=attempt,
                    reference_id=f"{req.public_ref}-{attempt}",
                    amount_paise=req.price_paise,
                    currency=req.currency,
                    created_by=created_by,
                    expires_at=now + timedelta(hours=settings.PAYMENT_LINK_EXPIRY_HOURS),
                )
        if to_cancel:
            self.gw.cancel_payment_link(to_cancel)
        return self._create_at_gateway(link)

    def _create_at_gateway(self, link: PaymentLink) -> PaymentLink:
        """PENDING -> ACTIVE. Safe to retry: the gateway call is idempotent on reference_id."""
        req = link.report_request
        try:
            created = self.gw.create_payment_link(
                reference_id=link.reference_id,
                amount_paise=link.amount_paise,
                currency=link.currency,
                description=f"Credit report {req.public_ref}",
                customer_phone=req.customer.wa_id,
                expires_at=link.expires_at,
            )
        except PermanentError as exc:
            link.status = PaymentLinkStatus.FAILED
            link.save(update_fields=["status"])
            audit.record(
                "payment_link.failed", actor_type=ActorType.SYSTEM, obj=link, changes={"code": exc.code}
            )
            raise
        link.razorpay_link_id, link.short_url = created.link_id, created.short_url
        link.status = PaymentLinkStatus.ACTIVE
        link.save(update_fields=["razorpay_link_id", "short_url", "status"])
        audit.record(
            "payment_link.created",
            actor_type=ActorType.SYSTEM,
            obj=link,
            changes={
                "reference_id": link.reference_id,
                "amount_paise": link.amount_paise,
                "created_by": str(link.created_by_id or "system"),
            },
        )
        return link

    def send_link(self, link: PaymentLink, *, sent_by=None, is_reminder=False):
        """Send (or resend) the link on WhatsApp. Each send has its own idempotency key."""
        req = link.report_request
        amount = format_rupees(link.amount_paise)
        msg = self.messaging.send(
            req.customer,
            text=f"Your credit report ({req.public_ref}) is ready. Pay {amount} here to receive the PDF: "
            f"{link.short_url}",
            template_key="payment_reminder" if is_reminder else "payment_link",
            template_params=[req.public_ref, amount, link.short_url],
            idempotency_key=f"paylink:{link.pk}:{link.sent_count + 1}",
            related_request=req,
            sent_by=sent_by,
            is_reminder=is_reminder,
        )
        PaymentLink.objects.filter(pk=link.pk).update(
            sent_count=link.sent_count + 1, last_sent_at=timezone.now()
        )
        link.refresh_from_db()
        return msg

    def sync_link(self, razorpay_link_id: str) -> PaymentLink | None:
        """Used by webhooks and reconciliation. Returns None if the link is not ours."""
        link = PaymentLink.objects.filter(razorpay_link_id=razorpay_link_id).first()
        if link is None:
            return None
        if link.status == PaymentLinkStatus.PAID:
            return link
        try:
            state = self.gw.fetch_payment_link(razorpay_link_id)
        except PermanentError as exc:
            raise LinkLookupFailed(exc) from exc
        if state.status == "paid":
            return self.confirm_link_payment(razorpay_link_id)
        final = {"expired": PaymentLinkStatus.EXPIRED, "cancelled": PaymentLinkStatus.CANCELLED}.get(
            state.status
        )
        if final and link.status != final:
            with transaction.atomic():
                link = PaymentLink.objects.select_for_update().get(pk=link.pk)
                if link.status != PaymentLinkStatus.PAID:
                    previous = link.status
                    link.status = final
                    link.save(update_fields=["status"])
                    audit.record(
                        "payment_link.synced",
                        actor_type=ActorType.SYSTEM,
                        obj=link,
                        changes={"from": previous, "to": final},
                    )
        return link

    # -- confirmation ------------------------------------------------------------------------------

    def confirm_link_payment(self, razorpay_link_id: str) -> PaymentLink:
        """Verify with the gateway and record the payment. Returns the link (status PAID if verified)."""
        link = PaymentLink.objects.select_related("report_request").get(razorpay_link_id=razorpay_link_id)
        if link.status == PaymentLinkStatus.PAID:
            return link
        state = self.gw.fetch_payment_link(razorpay_link_id)  # authoritative, outside any DB lock
        if state.status != "paid":
            return link  # nothing to do yet (e.g. partially_paid, created)

        captured = [p for p in state.payments if p.status == "captured"]
        problems = []
        if state.reference_id != link.reference_id:
            problems.append("reference_id")
        if state.currency != link.currency or any(p.currency != link.currency for p in captured):
            problems.append("currency")
        if (
            sum(p.amount_paise for p in captured) != link.amount_paise
            or state.amount_paise != link.amount_paise
        ):
            problems.append("amount")
        if problems:
            audit.record(
                "payment.mismatch",
                actor_type=ActorType.SYSTEM,
                obj=link,
                changes={
                    "problems": problems,
                    "gateway_status": state.status,
                    "gateway_amount_paid": state.amount_paid_paise,
                },
            )
            raise PaymentMismatch(",".join(problems))

        with transaction.atomic():
            link = PaymentLink.objects.select_for_update().get(pk=link.pk)
            if link.status == PaymentLinkStatus.PAID:
                return link
            for p in captured:
                Payment.objects.get_or_create(
                    razorpay_payment_id=p.payment_id,
                    defaults={
                        "payment_link": link,
                        "amount_paise": p.amount_paise,
                        "currency": p.currency,
                        "method": p.method,
                        "razorpay_order_id": p.order_id,
                        "status": PaymentStatus.CAPTURED,
                        "captured_at": timezone.now(),
                    },
                )
            duplicate = link.report_request.payment_links.filter(status=PaymentLinkStatus.PAID).exists()
            link.status, link.paid_at = PaymentLinkStatus.PAID, timezone.now()
            link.save(update_fields=["status", "paid_at"])
            audit.record(
                "payment.captured",
                actor_type=ActorType.SYSTEM,
                obj=link,
                changes={"amount_paise": link.amount_paise, "payments": [p.payment_id for p in captured]},
            )
            if duplicate:
                # Customer paid two links for one report: record it and flag for a refund, don't hide it.
                audit.record(
                    "payment.duplicate",
                    actor_type=ActorType.SYSTEM,
                    obj=link,
                    changes={"report_request": link.report_request.public_ref},
                )
        return link


# -- refunds (request -> four-eyes approval -> gateway) ------------------------------------------------


class RefundNotAllowed(PermanentError):
    def __init__(self, reason: str):
        super().__init__(f"refund not allowed: {reason}", code=f"refund_{reason}")


IN_FLIGHT_REFUNDS = ("requested", "approved", "processing")


class RefundService:
    def __init__(self, gateway=None):
        self.gw = gateway or get_payment_gateway()

    def request(self, payment_id, *, amount_paise: int, reason: str, by_user):
        from .models import Refund

        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(pk=payment_id)
            if payment.status not in {PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED}:
                raise RefundNotAllowed("payment_not_captured")
            pending = sum(
                payment.refunds.filter(status__in=IN_FLIGHT_REFUNDS).values_list("amount_paise", flat=True)
            )
            if amount_paise <= 0 or amount_paise > payment.refundable_paise - pending:
                raise RefundNotAllowed("amount_exceeds_refundable")
            refund = Refund.objects.create(
                payment=payment, amount_paise=amount_paise, reason=reason, requested_by=by_user
            )
            audit.record(
                "refund.requested",
                actor=by_user,
                obj=refund,
                changes={"amount_paise": amount_paise, "payment": payment.razorpay_payment_id},
            )
        return refund

    def decide(self, refund_id, *, approve: bool, by_user, note: str = ""):
        from .models import Refund, RefundStatus

        with transaction.atomic():
            refund = Refund.objects.select_for_update().get(pk=refund_id)
            if refund.status != RefundStatus.REQUESTED:
                raise RefundNotAllowed("already_decided")
            if refund.requested_by_id == by_user.pk:
                raise RefundNotAllowed("self_approval")  # also enforced by a DB constraint
            refund.status = RefundStatus.APPROVED if approve else RefundStatus.REJECTED
            refund.approved_by, refund.decided_at = by_user, timezone.now()
            if not approve:
                refund.failure_reason = note[:255]
            refund.save()
            audit.record(
                "refund.approved" if approve else "refund.rejected",
                actor=by_user,
                obj=refund,
                changes={"note": note},
            )
        return refund

    def process(self, refund_id):
        """APPROVED -> PROCESSING -> (PROCESSED now, or later via webhook). Safe to retry.

        A retry after an ambiguous failure (timeout) first asks the gateway whether our refund already
        exists, so a customer is never refunded twice.
        """
        from .models import Refund, RefundStatus

        with transaction.atomic():
            refund = Refund.objects.select_for_update().select_related("payment").get(pk=refund_id)
            if refund.status == RefundStatus.APPROVED:
                refund.status = RefundStatus.PROCESSING
                refund.save(update_fields=["status", "updated_at"])
            elif refund.status != RefundStatus.PROCESSING or refund.razorpay_refund_id:
                return refund  # done, failed, or already submitted (webhook will finish it)
        payment_id, reference = refund.payment.razorpay_payment_id, str(refund.pk)
        try:
            result = self.gw.find_refund(payment_id, reference) or self.gw.create_refund(
                payment_id, refund.amount_paise, reference=reference
            )
        except PermanentError as exc:
            Refund.objects.filter(pk=refund.pk).update(status=RefundStatus.FAILED, failure_reason=exc.code)
            audit.record("refund.failed", actor_type=ActorType.SYSTEM, obj=refund, changes={"code": exc.code})
            raise
        return self.apply_gateway_status(refund.pk, result.refund_id, result.status)

    def apply_gateway_status(self, refund_pk, razorpay_refund_id: str, gateway_status: str):
        """Record the gateway's view of a refund (from the API response or a refund.* webhook). Idempotent."""
        from .models import Refund, RefundStatus

        with transaction.atomic():
            refund = Refund.objects.select_for_update().get(pk=refund_pk)
            if refund.status in {RefundStatus.PROCESSED, RefundStatus.FAILED}:
                return refund
            refund.razorpay_refund_id = refund.razorpay_refund_id or razorpay_refund_id
            if gateway_status == "processed":
                payment = Payment.objects.select_for_update().get(pk=refund.payment_id)
                refund.status, refund.processed_at = RefundStatus.PROCESSED, timezone.now()
                payment.amount_refunded_paise += refund.amount_paise
                payment.status = (
                    PaymentStatus.REFUNDED
                    if payment.amount_refunded_paise >= payment.amount_paise
                    else PaymentStatus.PARTIALLY_REFUNDED
                )
                payment.save(update_fields=["amount_refunded_paise", "status", "updated_at"])
            elif gateway_status == "failed":
                refund.status, refund.failure_reason = RefundStatus.FAILED, "gateway_failed"
            refund.save()
            audit.record(
                f"refund.{refund.status}",
                actor_type=ActorType.SYSTEM,
                obj=refund,
                changes={"gateway_status": gateway_status},
            )
        return refund

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.audit import service as audit
from apps.audit.models import ActorType
from apps.core.integrations import IntegrationError
from apps.core.models import WebhookEvent, WebhookProcessingStatus
from apps.core.tasks import run_with_retries
from apps.followups.services import FollowUpService
from apps.reports.models import ReportRequest

from .models import PaymentLink, PaymentLinkStatus, Refund
from .services import LinkLookupFailed, PaymentService, RefundService

logger = logging.getLogger(__name__)

MAX_WEBHOOK_ATTEMPTS = 10


def after_paid(link: PaymentLink) -> str:
    """Shared by webhook, confirm task and reconciliation: close the follow-up and deliver the report."""
    if link is None or link.status != PaymentLinkStatus.PAID:
        return "not_paid"
    req = ReportRequest.objects.get(pk=link.report_request_id)
    FollowUpService().close_paid(req)
    from apps.reports.tasks import deliver_report_task

    deliver_report_task.delay(str(req.pk))
    return "paid"


@shared_task(bind=True, max_retries=5, name="payments.create_payment_link")
def create_payment_link_task(self, request_id: str):
    """GENERATED report -> payment link -> follow-up opened -> link sent on WhatsApp."""

    def run():
        service = PaymentService()
        link = service.get_or_create_link(request_id)
        FollowUpService().open_for(link.report_request)
        if link.sent_count == 0:
            service.send_link(link)
        return str(link.pk)

    return run_with_retries(self, run)


@shared_task(bind=True, max_retries=8, name="payments.confirm_payment")
def confirm_payment_task(self, razorpay_link_id: str):
    """Sync one link with the gateway; if paid, close the follow-up and deliver.

    If the gateway refuses the lookup, that is logged in one line instead of a traceback every
    reconciliation run. If it says the link does not exist at all, the link can never be paid: it is
    cancelled (audited) so reconciliation stops asking, and the next send creates a fresh link.
    Other errors (amount mismatch, delivery failure) still raise.
    """
    try:
        return run_with_retries(self, lambda: after_paid(PaymentService().sync_link(razorpay_link_id)))
    except LinkLookupFailed as exc:
        if exc.code == "link_not_found":
            _cancel_missing_link(razorpay_link_id)
        logger.warning("payment link %s not synced: %s", razorpay_link_id, exc.code)
        return f"error:{exc.code}"


def _cancel_missing_link(razorpay_link_id: str) -> None:
    with transaction.atomic():
        link = (
            PaymentLink.objects.select_for_update()
            .filter(razorpay_link_id=razorpay_link_id, status=PaymentLinkStatus.ACTIVE)
            .first()
        )
        if link is None:
            return
        link.status = PaymentLinkStatus.CANCELLED
        link.save(update_fields=["status"])
        audit.record(
            "payment_link.missing_at_gateway",
            actor_type=ActorType.SYSTEM,
            obj=link,
            changes={"from": PaymentLinkStatus.ACTIVE, "to": PaymentLinkStatus.CANCELLED},
        )


@shared_task(bind=True, max_retries=6, name="payments.process_refund")
def process_refund_task(self, refund_id: str):
    refund = run_with_retries(self, lambda: RefundService().process(refund_id))
    return refund.status if refund else None


# -- webhooks ---------------------------------------------------------------------------------------------


def _handle_event(event: WebhookEvent) -> str:
    """Return a WebhookProcessingStatus value. Webhook bodies are hints; the gateway API is the truth."""
    etype, data = event.event_type, event.payload
    if etype.startswith("payment_link."):
        link_id = (data.get("payment_link") or {}).get("id")
        if not link_id:
            return WebhookProcessingStatus.IGNORED
        link = PaymentService().sync_link(link_id)
        if link is None:
            return WebhookProcessingStatus.IGNORED  # not one of ours
        after_paid(link)
        return WebhookProcessingStatus.PROCESSED
    if etype.startswith("refund."):
        entity = data.get("refund") or {}
        refund = Refund.objects.filter(razorpay_refund_id=entity.get("id")).first()
        if refund is None and entity.get("refund_ref"):
            refund = Refund.objects.filter(pk=entity["refund_ref"]).first()
        if refund is None:
            return WebhookProcessingStatus.IGNORED
        RefundService().apply_gateway_status(refund.pk, entity.get("id", ""), entity.get("status", ""))
        return WebhookProcessingStatus.PROCESSED
    # payment.* events are covered by the payment_link.* events and by reconciliation.
    return WebhookProcessingStatus.IGNORED


@shared_task(bind=True, max_retries=6, name="payments.process_webhook_event")
def process_webhook_event_task(self, event_pk: int):
    WebhookEvent.objects.filter(pk=event_pk).update(
        processing_status=WebhookProcessingStatus.PROCESSING, attempts=F("attempts") + 1
    )
    event = WebhookEvent.objects.get(pk=event_pk)

    def run():
        status = _handle_event(event)
        WebhookEvent.objects.filter(pk=event_pk).update(
            processing_status=status, processed_at=timezone.now(), last_error=""
        )
        return status

    def give_up(exc):
        WebhookEvent.objects.filter(pk=event_pk).update(
            processing_status=WebhookProcessingStatus.FAILED, last_error=exc.code
        )

    try:
        return run_with_retries(self, run, on_give_up=give_up)
    except IntegrationError as exc:  # permanent (e.g. amount mismatch): keep for staff review, don't retry
        give_up(exc)
        return WebhookProcessingStatus.FAILED


@shared_task(name="payments.retry_failed_webhook_events")
def retry_failed_webhook_events():
    """Beat: requeue failed events and ones whose enqueue was lost (stuck RECEIVED/PROCESSING)."""
    stale = timezone.now() - timedelta(minutes=5)
    events = WebhookEvent.objects.filter(
        processing_status__in=[
            WebhookProcessingStatus.FAILED,
            WebhookProcessingStatus.RECEIVED,
            WebhookProcessingStatus.PROCESSING,
        ],
        received_at__lt=stale,
        attempts__lt=MAX_WEBHOOK_ATTEMPTS,
        signature_valid=True,
    ).values_list("pk", flat=True)[:200]
    count = 0
    for pk in events:
        process_webhook_event_task.delay(pk)
        count += 1
    return count


@shared_task(name="payments.reconcile_payment_links")
def reconcile_payment_links():
    """Beat: ask the gateway about open links so a missed webhook never leaves a paid customer waiting."""
    now = timezone.now()
    links = (
        PaymentLink.objects.filter(
            status=PaymentLinkStatus.ACTIVE,
            razorpay_link_id__isnull=False,
            created_at__lt=now - timedelta(minutes=2),
        )
        .exclude(expires_at__lt=now - timedelta(days=1))
        .values_list("razorpay_link_id", flat=True)[:500]
    )
    count = 0
    for link_id in links:
        confirm_payment_task.delay(link_id)
        count += 1
    return count

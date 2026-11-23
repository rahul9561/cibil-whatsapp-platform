"""Scheduled housekeeping for report requests and documents (ARCHITECTURE §6.3, §11)."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit import service as audit
from apps.audit.models import ActorType
from apps.core.integrations import IntegrationError
from apps.payments.models import PaymentLinkStatus

from .models import InvalidTransition, ReportDocument, ReportRequest, ReportStatus
from .services import ReportService
from .storage import ReportStorage

logger = logging.getLogger(__name__)

ABANDONABLE = [ReportStatus.DRAFT, ReportStatus.CONSENTED, ReportStatus.OTP_SENT]
FETCH_STUCK_AFTER = timedelta(minutes=10)


def _reset_conversation(req: ReportRequest, message: str, key: str) -> None:
    from apps.conversations.models import Conversation
    from apps.conversations.models import ConversationState as S
    from apps.messaging.services import MessagingService

    Conversation.objects.filter(customer_id=req.customer_id, active_report_request=req).update(
        state=S.ENDED, state_entered_at=timezone.now(), active_report_request=None
    )
    try:
        MessagingService().send(
            req.customer, text=message, idempotency_key=f"notify:{req.pk}:{key}", related_request=req
        )
    except IntegrationError as exc:  # outside the 24 h window: they'll see a fresh start when they write
        logger.info("expiry notice not sent: %s", exc.code)


@shared_task(name="reports.expire_stale_requests")
def expire_stale_requests() -> dict:
    from apps.conversations import copy as t
    from apps.conversations.models import Conversation

    from .tasks import fetch_report_task

    now = timezone.now()
    stale_before = now - timedelta(minutes=settings.STALE_REQUEST_MINUTES)
    service = ReportService()
    result = {"abandoned": 0, "unpaid_expired": 0, "fetch_restarted": 0}

    # 1. Abandoned while collecting details / consent / OTP. The report status doesn't move while the
    #    customer types their details, so the conversation's last message counts as activity too.
    for req in ReportRequest.objects.filter(
        report_status__in=ABANDONABLE, status_changed_at__lt=stale_before
    ):
        last_seen = (
            Conversation.objects.filter(customer_id=req.customer_id)
            .values_list("last_inbound_at", flat=True)
            .first()
        )
        if last_seen and last_seen >= stale_before:
            continue
        try:
            service.expire(req.pk, reason="abandoned")
        except InvalidTransition:
            continue  # moved on meanwhile
        _reset_conversation(req, t.REQUEST_TIMED_OUT, "timed_out")
        result["abandoned"] += 1

    # 2. Generated but never paid within its validity window.
    unpaid = ReportRequest.objects.filter(report_status=ReportStatus.GENERATED, expires_at__lt=now).exclude(
        payment_links__status=PaymentLinkStatus.PAID
    )
    for req in unpaid:
        try:
            service.expire(req.pk, reason="unpaid")
        except InvalidTransition:
            continue
        _reset_conversation(req, t.REPORT_EXPIRED.format(ref=req.public_ref), "expired")
        result["unpaid_expired"] += 1

    # 3. Self-healing: a fetch whose task was lost (worker crash, broker restart). Fetch is exactly-once,
    #    so restarting it is safe: it resumes from FETCHING instead of pulling a second report.
    stuck = ReportRequest.objects.filter(
        report_status__in=[ReportStatus.OTP_VERIFIED, ReportStatus.FETCHING],
        status_changed_at__lt=now - FETCH_STUCK_AFTER,
    )
    for pk in stuck.values_list("pk", flat=True)[:100]:
        fetch_report_task.delay(str(pk))
        result["fetch_restarted"] += 1
    return result


@shared_task(name="reports.purge_expired_documents")
def purge_expired_documents() -> int:
    """Delete report files past retention. Metadata (hash, size, dates) stays for the audit trail."""
    storage = ReportStorage()
    purged = 0
    due = ReportDocument.objects.filter(purge_after__lte=timezone.now(), purged_at__isnull=True)
    for doc_pk in due.values_list("pk", flat=True)[:1000]:
        with transaction.atomic():
            doc = ReportDocument.objects.select_for_update().get(pk=doc_pk)
            if doc.purged_at:
                continue
            existed = storage.delete(doc.storage_key)
            doc.purged_at = timezone.now()
            doc.wa_media_id = ""
            doc.save(update_fields=["purged_at", "wa_media_id", "updated_at"])
            audit.record(
                "report.document_purged",
                actor_type=ActorType.SYSTEM,
                obj=doc.report_request,
                changes={"file_existed": existed},
            )
        purged += 1
    return purged

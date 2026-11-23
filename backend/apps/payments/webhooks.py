"""Razorpay webhook receiver (ARCHITECTURE §6.1).

Verify signature on the raw body -> store the event once (unique source+event id) -> queue processing ->
return 200 fast. The payload is reduced to an allowlist of non-personal fields before it is stored.
"""

import hashlib
import json
import logging

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.audit import service as audit
from apps.audit.models import ActorType
from apps.core.models import WebhookEvent, WebhookSource

from .gateway import get_payment_gateway

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
ENTITY_FIELDS = {
    "payment_link": {"id", "status", "amount", "amount_paid", "currency", "reference_id"},
    "payment": {"id", "status", "amount", "amount_refunded", "currency", "method", "order_id", "error_code"},
    "refund": {"id", "status", "amount", "currency", "payment_id"},
    "order": {"id", "status", "amount", "amount_paid", "currency"},
}


def sanitize(payload: dict) -> dict:
    """Keep ids, amounts and statuses only: never contact details, card/VPA data or free-text notes."""
    clean = {"event": str(payload.get("event", ""))[:100], "created_at": payload.get("created_at")}
    entities = payload.get("payload") or {}
    for name, allowed in ENTITY_FIELDS.items():
        entity = (entities.get(name) or {}).get("entity") or {}
        if entity:
            clean[name] = {k: entity[k] for k in allowed if k in entity}
            notes = entity.get("notes") or {}
            if isinstance(notes, dict) and "refund_ref" in notes:  # our own reference, set by RefundService
                clean[name]["refund_ref"] = str(notes["refund_ref"])[:64]
    return clean


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    body = request.body
    if len(body) > MAX_BODY_BYTES:
        return JsonResponse({"detail": "payload too large"}, status=413)

    signature = request.headers.get("X-Razorpay-Signature", "")
    if not get_payment_gateway().verify_webhook_signature(body, signature):
        logger.warning("razorpay webhook rejected: invalid signature")
        audit.record(
            "webhook.signature_invalid",
            request=request,
            actor_type=ActorType.WEBHOOK,
            changes={"source": "razorpay", "size": len(body)},
        )
        return JsonResponse({"detail": "invalid signature"}, status=400)

    try:
        payload = json.loads(body)
    except ValueError:
        return JsonResponse({"detail": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"detail": "invalid payload"}, status=400)

    event_id = request.headers.get("X-Razorpay-Event-Id") or "sha256:" + hashlib.sha256(body).hexdigest()
    try:
        with transaction.atomic():
            event = WebhookEvent.objects.create(
                source=WebhookSource.RAZORPAY,
                event_id=event_id[:128],
                event_type=str(payload.get("event", ""))[:100],
                signature_valid=True,
                payload=sanitize(payload),
            )
    except IntegrityError:
        return JsonResponse({"status": "duplicate"})  # already have it: Razorpay retries are harmless

    from .tasks import process_webhook_event_task

    transaction.on_commit(lambda: process_webhook_event_task.delay(event.pk))
    return JsonResponse({"status": "accepted"})

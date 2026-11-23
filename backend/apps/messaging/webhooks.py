"""WhatsApp Cloud API webhook (ARCHITECTURE §5, §6.5).

GET  = Meta's subscription handshake (hub.verify_token must match).
POST = signed notifications (X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(app secret, raw body)).
Each message/status becomes one WebhookEvent (deduplicated); processing happens in a Celery task.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.audit import service as audit
from apps.audit.models import ActorType
from apps.core.crypto import encrypt_str
from apps.core.models import WebhookEvent, WebhookSource

from .inbound import park_body, parse

logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 512 * 1024


def valid_signature(body: bytes, header: str) -> bool:
    secret = settings.WHATSAPP_APP_SECRET
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256=") :].strip())


def _store(event_id: str, event_type: str, payload: dict) -> WebhookEvent | None:
    try:
        with transaction.atomic():
            return WebhookEvent.objects.create(
                source=WebhookSource.WHATSAPP,
                event_id=event_id[:128],
                event_type=event_type[:100],
                signature_valid=True,
                payload=payload,
            )
    except IntegrityError:
        return None  # Meta retried a notification we already have


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    if request.method == "GET":
        token = request.GET.get("hub.verify_token", "")
        expected = settings.WHATSAPP_VERIFY_TOKEN
        if request.GET.get("hub.mode") == "subscribe" and expected and hmac.compare_digest(token, expected):
            return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
        return HttpResponse(status=403)

    body = request.body
    if len(body) > MAX_BODY_BYTES:
        return JsonResponse({"detail": "payload too large"}, status=413)
    if not valid_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("whatsapp webhook rejected: invalid signature")
        audit.record(
            "webhook.signature_invalid",
            request=request,
            actor_type=ActorType.WEBHOOK,
            changes={"source": "whatsapp", "size": len(body)},
        )
        return JsonResponse({"detail": "invalid signature"}, status=400)
    try:
        payload = json.loads(body)
        messages, statuses = parse(payload if isinstance(payload, dict) else {})
    except ValueError:
        return JsonResponse({"detail": "invalid json"}, status=400)

    from .tasks import process_whatsapp_event_task

    created = []
    for m in messages:
        # Stored: who (encrypted) and what kind. The text itself only waits briefly in the cache.
        event = _store(
            m.wa_message_id,
            f"message.{m.type}",
            {
                "wa_message_id": m.wa_message_id,
                "from_enc": encrypt_str(m.wa_id),
                "type": m.type,
                "reply_id": (m.reply_id or "")[:256],
                "timestamp": m.timestamp.isoformat(),
            },
        )
        if event:
            park_body(m.wa_message_id, m.text)
            created.append(event.pk)
    for s in statuses:
        event = _store(
            f"status:{s.wa_message_id}:{s.status}",
            f"status.{s.status}",
            {
                "wa_message_id": s.wa_message_id,
                "status": s.status,
                "timestamp": s.timestamp.isoformat(),
                "error_code": s.error_code,
            },
        )
        if event:
            created.append(event.pk)
    for pk in created:
        transaction.on_commit(lambda pk=pk: process_whatsapp_event_task.delay(pk))
    return JsonResponse({"status": "accepted", "events": len(created)})

"""Parsing WhatsApp webhook payloads and applying delivery statuses.

Message *text* never goes to the database or the broker. It is parked in the cache for a few minutes
under the message id and consumed (read + deleted) by the worker; it may be an OTP (ARCHITECTURE §6.5).
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from .models import MessageLog, MessageStatus

_BODY_KEY = "wa:inbound:"


@dataclass(frozen=True)
class InboundMessage:
    wa_message_id: str
    wa_id: str
    type: str
    text: str | None  # typed text or button title
    reply_id: str | None  # our own button id (non-personal)
    timestamp: datetime


@dataclass(frozen=True)
class StatusUpdate:
    wa_message_id: str
    status: str
    timestamp: datetime
    error_code: str


def _ts(value) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(tz=UTC)


def parse(payload: dict) -> tuple[list[InboundMessage], list[StatusUpdate]]:
    messages, statuses = [], []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if (
                value.get("metadata", {}).get("phone_number_id")
                not in (None, settings.WHATSAPP_PHONE_NUMBER_ID)
                and settings.WHATSAPP_PHONE_NUMBER_ID
            ):
                continue  # a different number on the same app: not ours
            for m in value.get("messages") or []:
                mtype = m.get("type", "unknown")
                text, reply_id = None, None
                if mtype == "text":
                    text = (m.get("text") or {}).get("body")
                elif mtype == "interactive":
                    inter = m.get("interactive") or {}
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    reply_id, text = reply.get("id"), reply.get("title")
                elif mtype == "button":  # quick-reply button on a template
                    btn = m.get("button") or {}
                    reply_id, text = btn.get("payload"), btn.get("text")
                if m.get("id") and m.get("from"):
                    messages.append(
                        InboundMessage(m["id"], m["from"], mtype, text, reply_id, _ts(m.get("timestamp")))
                    )
            for s in value.get("statuses") or []:
                if s.get("id") and s.get("status"):
                    errors = s.get("errors") or [{}]
                    statuses.append(
                        StatusUpdate(
                            s["id"], s["status"], _ts(s.get("timestamp")), str(errors[0].get("code") or "")
                        )
                    )
    return messages, statuses


def park_body(wa_message_id: str, text: str | None) -> None:
    if text is not None:
        cache.set(_BODY_KEY + wa_message_id, text, timeout=settings.WHATSAPP_INBOUND_BODY_TTL)


def take_body(wa_message_id: str) -> str | None:
    """Read once: the body is deleted as soon as the worker has it."""
    key = _BODY_KEY + wa_message_id
    text = cache.get(key)
    cache.delete(key)
    return text


_RANK = {MessageStatus.QUEUED: 0, MessageStatus.SENT: 1, MessageStatus.DELIVERED: 2, MessageStatus.READ: 3}


def apply_status(update: StatusUpdate) -> MessageLog | None:
    """Advance a message's status (never backwards) and mirror delivery onto the report request."""
    from apps.reports.models import DeliveryStatus, ReportRequest

    with transaction.atomic():
        msg = MessageLog.objects.select_for_update().filter(wa_message_id=update.wa_message_id).first()
        if msg is None:
            return None
        if update.status == "failed":
            msg.status, msg.failed_at, msg.error_code = (
                MessageStatus.FAILED,
                update.timestamp,
                update.error_code,
            )
        elif update.status in {"sent", "delivered", "read"}:
            new = MessageStatus(update.status)
            if _RANK.get(new, 0) > _RANK.get(msg.status, -1):
                msg.status = new
            setattr(
                msg,
                {"sent": "sent_at", "delivered": "delivered_at", "read": "read_at"}[update.status],
                update.timestamp,
            )
        else:
            return msg
        msg.save()

        if msg.related_report_request_id and (msg.idempotency_key or "").startswith("deliver:"):
            req = ReportRequest.objects.select_for_update().get(pk=msg.related_report_request_id)
            target = {
                "delivered": DeliveryStatus.DELIVERED,
                "read": DeliveryStatus.READ,
                "failed": DeliveryStatus.FAILED,
            }.get(update.status)
            order = [DeliveryStatus.SENT, DeliveryStatus.DELIVERED, DeliveryStatus.READ]
            if target == DeliveryStatus.FAILED or (
                target in order
                and req.delivery_status in order
                and order.index(target) > order.index(req.delivery_status)
            ):
                req.delivery_status = target
                if target in {DeliveryStatus.DELIVERED, DeliveryStatus.READ} and not req.delivered_at:
                    req.delivered_at = update.timestamp
                req.save()  # DELIVERED/READ releases the customer's open-request lock
    return msg

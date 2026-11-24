"""Local stand-in for Meta delivering a customer message (mock WhatsApp gateway only)."""

import secrets

from django.conf import settings
from django.utils import timezone

from apps.core.crypto import encrypt_str, normalize_phone_in
from apps.core.models import WebhookEvent, WebhookSource

from .gateway.mock import outbox
from .inbound import park_body


def simulate_inbound(
    wa_id: str, text: str | None = None, reply_id: str | None = None, msg_type: str | None = None
) -> list[dict]:
    """Run one message through the same storage + processing path as the webhook; return replies."""
    if settings.WHATSAPP_GATEWAY != "mock":
        raise RuntimeError("simulate_inbound only works with WHATSAPP_GATEWAY=mock")
    from apps.conversations.bot import process_inbound_event

    wa_id = normalize_phone_in(wa_id)  # Meta always sends full international digits
    msg_type = msg_type or ("interactive" if reply_id else "text")
    wamid = "wamid.sim_" + secrets.token_hex(26)  # as long as a real wamid (~60 chars), so length bugs show
    event = WebhookEvent.objects.create(
        source=WebhookSource.WHATSAPP,
        event_id=wamid,
        event_type=f"message.{msg_type}",
        signature_valid=True,
        payload={
            "wa_message_id": wamid,
            "from_enc": encrypt_str(wa_id),
            "type": msg_type,
            "reply_id": reply_id or "",
            "timestamp": timezone.now().isoformat(),
        },
    )
    park_body(wamid, text)
    before = len(outbox())
    process_inbound_event(event)
    return [m for m in outbox()[before:] if m["to"] == wa_id]

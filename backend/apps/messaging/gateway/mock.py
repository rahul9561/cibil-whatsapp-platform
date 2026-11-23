"""In-memory WhatsApp gateway (dev and tests). Sent messages go to an outbox in the cache.

Recipients ending in "0000" fail permanently (e.g. not a WhatsApp number); set `MockWhatsAppGateway.fail_next`
to a TransientError/PermanentError instance to make the next call fail.
"""

import secrets

from django.core.cache import cache

from .base import Button, PermanentError, SendResult, WhatsAppGateway

OUTBOX_KEY = "mockwa:outbox"
MEDIA_KEY = "mockwa:media:"


def outbox() -> list[dict]:
    return list(cache.get(OUTBOX_KEY, []))


def clear_outbox() -> None:
    cache.delete(OUTBOX_KEY)


class MockWhatsAppGateway(WhatsAppGateway):
    name = "mock"
    fail_next: Exception | None = None

    def _record(self, to: str, kind: str, **data) -> SendResult:
        if MockWhatsAppGateway.fail_next is not None:
            exc, MockWhatsAppGateway.fail_next = MockWhatsAppGateway.fail_next, None
            raise exc
        if to.endswith("0000"):
            raise PermanentError("recipient is not a WhatsApp user", code="recipient_invalid")
        wamid = "wamid.mock_" + secrets.token_hex(8)
        box = outbox()
        box.append({"to": to, "kind": kind, "wa_message_id": wamid, **data})
        cache.set(OUTBOX_KEY, box, timeout=None)
        return SendResult(wamid)

    def send_text(self, to, body):
        return self._record(to, "text", body=body)

    def send_buttons(self, to, body, buttons: list[Button]):
        return self._record(to, "buttons", body=body, buttons=[b.id for b in buttons])

    def send_template(self, to, template, language, params, document_media_id=None, document_filename=None):
        return self._record(
            to,
            "template",
            template=template,
            language=language,
            params=list(params),
            media_id=document_media_id,
            filename=document_filename,
        )

    def upload_media(self, content: bytes, mime_type: str, filename: str) -> str:
        media_id = "media_mock_" + secrets.token_hex(6)
        cache.set(MEDIA_KEY + media_id, {"size": len(content), "mime": mime_type}, timeout=60 * 60)
        return media_id

    def send_document(self, to, media_id, filename, caption=""):
        if cache.get(MEDIA_KEY + media_id) is None:
            raise PermanentError("unknown media id", code="media_not_found")
        return self._record(to, "document", media_id=media_id, filename=filename, caption=caption)

"""WhatsApp Business Cloud API adapter (Meta Graph API).

Re-check against Meta's current documentation before go-live: the Graph API version (setting
WHATSAPP_GRAPH_VERSION), message payload shapes and the error codes mapped below.

- Messages:  POST {graph}/{version}/{phone_number_id}/messages
- Media:     POST {graph}/{version}/{phone_number_id}/media  (multipart; returns a media id, no public URL)
"""

import logging

import requests
from django.conf import settings

from .base import Button, OutsideServiceWindow, PermanentError, SendResult, TransientError, WhatsAppGateway

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
TIMEOUT = (5, 30)

# Meta error codes (re-verify): rate limits and temporary failures are retried; the rest are final.
RETRYABLE_CODES = {1, 2, 4, 80007, 130429, 131000, 131016, 131048, 131056, 133004}
REENGAGEMENT_CODE = 131047  # more than 24 h since the customer last wrote: a template is required
AUTH_CODES = {0, 10, 190}  # token invalid/expired/insufficient permission: needs an operator


class CloudApiWhatsAppGateway(WhatsAppGateway):
    name = "cloud_api"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.base = f"{GRAPH}/{settings.WHATSAPP_GRAPH_VERSION}/{settings.WHATSAPP_PHONE_NUMBER_ID}"
        self.headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}

    # -- transport --------------------------------------------------------------------------------------

    def _post(self, path: str, *, json=None, data=None, files=None) -> dict:
        try:
            resp = self.session.post(
                f"{self.base}/{path}",
                json=json,
                data=data,
                files=files,
                headers=self.headers,
                timeout=TIMEOUT,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            raise TransientError("whatsapp: network error", code="whatsapp_unreachable") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code < 300:
            return body
        error = body.get("error") or {}
        code = error.get("code")
        if resp.status_code >= 500 or resp.status_code == 429 or code in RETRYABLE_CODES:
            raise TransientError(
                f"whatsapp: temporary error {code}", code=f"whatsapp_{code or resp.status_code}"
            )
        if code == REENGAGEMENT_CODE:
            raise OutsideServiceWindow("whatsapp: customer service window closed")
        if code in AUTH_CODES:
            logger.error("whatsapp: authentication/permission error %s - check WHATSAPP_ACCESS_TOKEN", code)
            raise PermanentError("whatsapp: authentication error", code="whatsapp_auth")
        raise PermanentError(
            f"whatsapp: request rejected ({code})", code=f"whatsapp_{code or resp.status_code}"
        )

    def _send(self, to: str, message: dict) -> SendResult:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **message}
        body = self._post("messages", json=payload)
        try:
            return SendResult(body["messages"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            raise TransientError(
                "whatsapp: response without message id", code="whatsapp_bad_response"
            ) from exc

    # -- messages ---------------------------------------------------------------------------------------

    def send_text(self, to, body):
        return self._send(to, {"type": "text", "text": {"body": body[:4096], "preview_url": False}})

    def send_buttons(self, to, body, buttons: list[Button]):
        return self._send(
            to,
            {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body[:1024]},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": b.id[:256], "title": b.title[:20]}}
                            for b in buttons[:3]
                        ]
                    },
                },
            },
        )

    def send_template(self, to, template, language, params, document_media_id=None, document_filename=None):
        components = []
        if document_media_id:
            components.append(
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "document",
                            "document": {
                                "id": document_media_id,
                                "filename": document_filename or "file.pdf",
                            },
                        }
                    ],
                }
            )
        if params:
            components.append(
                {"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}
            )
        return self._send(
            to,
            {
                "type": "template",
                "template": {"name": template, "language": {"code": language}, "components": components},
            },
        )

    def upload_media(self, content: bytes, mime_type: str, filename: str) -> str:
        body = self._post(
            "media",
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, content, mime_type)},
        )
        if not body.get("id"):
            raise TransientError("whatsapp: media upload returned no id", code="whatsapp_bad_response")
        return body["id"]

    def send_document(self, to, media_id, filename, caption=""):
        return self._send(
            to,
            {
                "type": "document",
                "document": {"id": media_id, "filename": filename, "caption": caption[:1024]},
            },
        )

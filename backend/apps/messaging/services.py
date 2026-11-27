"""Outbound WhatsApp messaging with the 24-hour window rule, opt-out and idempotency (ARCHITECTURE §5)."""

import hashlib

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.conversations.models import Conversation
from apps.core.integrations import PermanentError, TransientError
from apps.core.redaction import scrub

from .gateway import get_whatsapp
from .gateway.base import Button, OutsideServiceWindow
from .models import Direction, MessageLog, MessageStatus, MessageType
from .templates import TemplateParamError, validate_params


class CustomerOptedOut(PermanentError):
    def __init__(self):
        super().__init__("customer opted out of non-essential messages", code="customer_opted_out")


class TemplateParamMismatch(PermanentError):
    """Parameters don't fit the approved template: refused before a log row or a gateway call."""

    def __init__(self, detail: str):
        super().__init__(detail, code="template_param_mismatch")


def checked_params(template_key: str, params) -> list[str]:
    try:
        return validate_params(template_key, params)
    except TemplateParamError as exc:
        raise TemplateParamMismatch(str(exc)) from exc


MAX_REPLY_BUTTONS = 3
IDEMPOTENCY_KEY_MAX = MessageLog._meta.get_field("idempotency_key").max_length


def storable_key(key: str | None) -> str | None:
    """Fit an idempotency key into the column. Real WhatsApp message ids ("wamid.HBgM…") are ~60 characters,
    so keys built from them overflow. Long keys are replaced by a stable hash: the same input always maps to
    the same key, so retries still find their row. Short keys are kept readable."""
    if not key or len(key) <= IDEMPOTENCY_KEY_MAX:
        return key
    prefix = key.split(":", 1)[0][:12]
    digest = hashlib.sha256(key.encode()).hexdigest()
    return f"{prefix}:h:{digest}"[:IDEMPOTENCY_KEY_MAX]


def in_service_window(customer) -> bool:
    conv = Conversation.objects.filter(customer=customer).only("last_inbound_at").first()
    return bool(conv and conv.in_service_window())


class MessagingService:
    def __init__(self, gateway=None):
        self.gw = gateway or get_whatsapp()

    # -- idempotent log row --------------------------------------------------------------------------

    def _claim(self, customer, *, idempotency_key, msg_type, related_request, sent_by, template_name, body):
        """Return (log_row, already_sent). Re-running with the same key never sends twice."""
        idempotency_key = storable_key(idempotency_key)
        if idempotency_key:
            existing = MessageLog.objects.filter(idempotency_key=idempotency_key).first()
            if existing and existing.status not in {MessageStatus.QUEUED, MessageStatus.FAILED}:
                return existing, True
            if existing:
                return existing, False
        try:
            with transaction.atomic():
                row = MessageLog.objects.create(
                    customer=customer,
                    direction=Direction.OUTBOUND,
                    idempotency_key=idempotency_key,
                    type=msg_type,
                    template_name=template_name,
                    body_redacted=body,
                    status=MessageStatus.QUEUED,
                    sent_by=sent_by,
                    related_report_request=related_request,
                )
            return row, False
        except IntegrityError:  # concurrent claim with the same key
            return self._claim(
                customer,
                idempotency_key=idempotency_key,
                msg_type=msg_type,
                related_request=related_request,
                sent_by=sent_by,
                template_name=template_name,
                body=body,
            )

    def _dispatch(self, row: MessageLog, call) -> MessageLog:
        try:
            result = call()
        except PermanentError as exc:
            row.status, row.error_code, row.failed_at = MessageStatus.FAILED, exc.code, timezone.now()
            row.save(update_fields=["status", "error_code", "failed_at", "updated_at"])
            raise
        except TransientError:
            raise  # row stays QUEUED; the retry reuses it via the idempotency key
        row.wa_message_id, row.status, row.sent_at = result.wa_message_id, MessageStatus.SENT, timezone.now()
        row.error_code = ""
        row.save(update_fields=["wa_message_id", "status", "sent_at", "error_code", "updated_at"])
        return row

    # -- public API ----------------------------------------------------------------------------------

    def send(
        self,
        customer,
        *,
        text: str | None = None,
        buttons: list[Button] | None = None,
        list_button: str = "",
        template_key: str | None = None,
        template_params: list[str] | tuple = (),
        idempotency_key: str | None = None,
        related_request=None,
        sent_by=None,
        is_reminder: bool = False,
    ) -> MessageLog:
        """Free-form text inside the 24 h window, otherwise the approved template (if one is given).

        More than 3 buttons are sent as a list menu opened by `list_button`."""
        if is_reminder and customer.is_opted_out:
            raise CustomerOptedOut()
        if template_key:  # validated even when free text wins, so a bad call site fails in tests too
            template_params = checked_params(template_key, template_params)
        window = in_service_window(customer)
        if text and window:
            mode = "buttons" if buttons else "text"
        elif template_key:
            mode = "template"
        else:
            raise OutsideServiceWindow()

        template_name = settings.WHATSAPP_TEMPLATES[template_key] if mode == "template" else ""
        body = scrub(text or "") if mode != "template" else scrub(" | ".join(map(str, template_params)))
        row, done = self._claim(
            customer,
            idempotency_key=idempotency_key,
            msg_type={
                "text": MessageType.TEXT,
                "buttons": MessageType.INTERACTIVE,
                "template": MessageType.TEMPLATE,
            }[mode],
            related_request=related_request,
            sent_by=sent_by,
            template_name=template_name,
            body=body,
        )
        if done:
            return row
        to = customer.wa_id
        if mode == "text":
            return self._dispatch(row, lambda: self.gw.send_text(to, text))
        if mode == "buttons" and len(buttons) > MAX_REPLY_BUTTONS:  # WhatsApp: 3 buttons, else a list
            return self._dispatch(row, lambda: self.gw.send_list(to, text, list_button or "Choose", buttons))
        if mode == "buttons":
            return self._dispatch(row, lambda: self.gw.send_buttons(to, text, buttons))
        return self._dispatch(
            row,
            lambda: self.gw.send_template(
                to, template_name, settings.WHATSAPP_TEMPLATE_LANGUAGE, template_params
            ),
        )

    def send_document(
        self,
        customer,
        *,
        content: bytes,
        filename: str,
        caption: str,
        template_params: list[str],
        idempotency_key: str,
        related_request=None,
        sent_by=None,
    ) -> MessageLog:
        """Upload to WhatsApp media (no public URL) and send; template outside the 24 h window."""
        template_params = checked_params("report_delivery", template_params)
        window = in_service_window(customer)
        template_name = "" if window else settings.WHATSAPP_TEMPLATES["report_delivery"]
        row, done = self._claim(
            customer,
            idempotency_key=idempotency_key,
            msg_type=MessageType.DOCUMENT if window else MessageType.TEMPLATE,
            related_request=related_request,
            sent_by=sent_by,
            template_name=template_name,
            body=f"[document] {filename}",
        )
        if done:
            return row
        media_id = self.gw.upload_media(content, "application/pdf", filename)
        to = customer.wa_id
        if window:
            return self._dispatch(row, lambda: self.gw.send_document(to, media_id, filename, caption))
        return self._dispatch(
            row,
            lambda: self.gw.send_template(
                to,
                template_name,
                settings.WHATSAPP_TEMPLATE_LANGUAGE,
                template_params,
                document_media_id=media_id,
                document_filename=filename,
            ),
        )

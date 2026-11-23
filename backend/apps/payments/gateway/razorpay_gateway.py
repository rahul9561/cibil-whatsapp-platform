"""Razorpay adapter (ARCHITECTURE §6.1). Uses the official `razorpay` SDK; no URLs are hand-written here.

Behaviour notes (re-check against Razorpay's current docs before go-live):
- Payment links are created with `reference_id` = our idempotency key. If a retry hits Razorpay's duplicate
  reference_id rejection, the existing link is looked up with `payment_link.all({"reference_id": ...})`.
- Payment amounts/currency are taken from `payment.fetch(id)` for each payment on the link, not from the
  link's summary, before we treat a link as paid.
- Webhook signature: hex HMAC-SHA256 of the raw request body with the webhook secret
  (`X-Razorpay-Signature`), compared in constant time on the raw bytes.
- Every call has an explicit timeout; the SDK has none by default. SDK-level retries stay off: Celery
  tasks retry with backoff instead.
"""

import hashlib
import hmac
import logging
from datetime import UTC, datetime

import requests
from django.conf import settings

from .base import (
    CreatedLink,
    GatewayPayment,
    LinkState,
    PaymentGateway,
    PermanentError,
    RefundResult,
    TransientError,
)

logger = logging.getLogger(__name__)

TIMEOUT = (5, 30)  # connect, read (seconds)


def _call(fn, *args, what: str, **kwargs):
    """Run an SDK call and translate failures into our transient/permanent error families."""
    import razorpay.errors as rz

    try:
        return fn(*args, timeout=TIMEOUT, **kwargs)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        raise TransientError(f"razorpay {what}: network error", code="razorpay_unreachable") from exc
    except (rz.ServerError, rz.GatewayError) as exc:
        raise TransientError(f"razorpay {what}: server error", code="razorpay_server_error") from exc
    except rz.BadRequestError as exc:
        raise PermanentError(f"razorpay {what}: {str(exc)[:200]}", code="razorpay_bad_request") from exc
    except ValueError as exc:  # non-JSON error page from a proxy/load balancer
        raise TransientError(f"razorpay {what}: unreadable response", code="razorpay_bad_response") from exc


class RazorpayGateway(PaymentGateway):
    name = "razorpay"

    def __init__(self, client=None):
        if client is None:
            import razorpay

            client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
            client.set_app_details({"title": "cibil-whatsapp-platform", "version": "1.0"})
        self.client = client

    # -- links ---------------------------------------------------------------------------------------------

    def create_payment_link(
        self, *, reference_id, amount_paise, currency, description, customer_phone, expires_at
    ):
        data = {
            "amount": amount_paise,
            "currency": currency,
            "accept_partial": False,
            "reference_id": reference_id,
            "description": description[:2048],
            "customer": {"contact": f"+{customer_phone}"},
            # We deliver the link ourselves on WhatsApp; Razorpay's own SMS/e-mail stay off.
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "expire_by": int(expires_at.timestamp()),
            "notes": {"reference_id": reference_id},
        }
        try:
            entity = _call(self.client.payment_link.create, data, what="create link")
        except PermanentError as exc:
            existing = self._find_by_reference(reference_id) if "reference" in str(exc).lower() else None
            if existing is None:
                raise
            entity = existing
        return CreatedLink(entity["id"], entity["short_url"], _ts(entity.get("expire_by")))

    def _find_by_reference(self, reference_id: str) -> dict | None:
        result = _call(self.client.payment_link.all, {"reference_id": reference_id}, what="find link")
        items = result.get("payment_links") or result.get("items") or []
        return next((i for i in items if i.get("reference_id") == reference_id), None)

    def fetch_payment_link(self, link_id):
        entity = _call(self.client.payment_link.fetch, link_id, what="fetch link")
        payments = []
        for p in entity.get("payments") or []:
            payment_id = p.get("payment_id") or p.get("id")
            if not payment_id:
                continue
            full = _call(self.client.payment.fetch, payment_id, what="fetch payment")  # authoritative
            payments.append(
                GatewayPayment(
                    payment_id=full["id"],
                    amount_paise=int(full["amount"]),
                    currency=full["currency"],
                    status=full["status"],
                    method=full.get("method") or "",
                    order_id=full.get("order_id") or "",
                )
            )
        return LinkState(
            link_id=entity["id"],
            reference_id=entity.get("reference_id") or "",
            status=entity["status"],
            amount_paise=int(entity["amount"]),
            amount_paid_paise=int(entity.get("amount_paid") or 0),
            currency=entity["currency"],
            payments=payments,
        )

    def cancel_payment_link(self, link_id):
        try:
            _call(self.client.payment_link.cancel, link_id, what="cancel link")
        except PermanentError:
            # Already paid/expired/cancelled at Razorpay: nothing to cancel. Reconciliation sorts out status.
            logger.info("razorpay cancel refused for link (already final)")

    # -- refunds -------------------------------------------------------------------------------------------

    def create_refund(self, payment_id, amount_paise, reference):
        entity = _call(
            self.client.payment.refund,
            payment_id,
            {"amount": amount_paise, "speed": "normal", "notes": {"refund_ref": reference}},
            what="refund",
        )
        return RefundResult(entity["id"], entity.get("status") or "pending")

    def find_refund(self, payment_id, reference):
        result = _call(self.client.payment.fetch_multiple_refund, payment_id, what="list refunds")
        for item in result.get("items") or []:
            if (item.get("notes") or {}).get("refund_ref") == reference:
                return RefundResult(item["id"], item.get("status") or "pending")
        return None

    # -- webhooks ------------------------------------------------------------------------------------------

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        return verify_signature(body, signature, settings.RAZORPAY_WEBHOOK_SECRET)


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def _ts(value) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=UTC) if value else None

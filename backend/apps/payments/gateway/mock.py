"""In-memory payment gateway (dev and tests). State lives in the cache.

`simulate_payment(link_id, amount_paise=None)` plays the customer paying; tests and the dev tools use it.
"""

import hashlib
import hmac
import secrets

from django.core.cache import cache

from .base import (
    CreatedLink,
    GatewayPayment,
    LinkState,
    PaymentGateway,
    PermanentError,
    RefundResult,
)

MOCK_WEBHOOK_SECRET = "mock-webhook-secret"  # noqa: S105 (test-only value)
_TTL = 60 * 60 * 24 * 7


def _link_key(link_id):
    return f"mockpay:link:{link_id}"


def _ref_key(reference_id):
    return f"mockpay:ref:{reference_id}"


def simulate_payment(link_id: str, amount_paise: int | None = None, currency: str = "INR") -> str:
    state = cache.get(_link_key(link_id))
    if state is None:
        raise PermanentError("unknown link", code="link_not_found")
    amount = state["amount_paise"] if amount_paise is None else amount_paise
    payment_id = "pay_mock_" + secrets.token_hex(7)
    state["payments"].append(
        {
            "payment_id": payment_id,
            "amount_paise": amount,
            "currency": currency,
            "status": "captured",
            "method": "upi",
        }
    )
    state["amount_paid_paise"] += amount
    state["status"] = "paid" if state["amount_paid_paise"] >= state["amount_paise"] else "partially_paid"
    cache.set(_link_key(link_id), state, timeout=_TTL)
    return payment_id


class MockPaymentGateway(PaymentGateway):
    name = "mock"

    def create_payment_link(
        self, *, reference_id, amount_paise, currency, description, customer_phone, expires_at
    ):
        existing = cache.get(_ref_key(reference_id))
        if existing:  # idempotent on reference_id
            state = cache.get(_link_key(existing))
            return CreatedLink(existing, state["short_url"], expires_at)
        link_id = "plink_mock_" + secrets.token_hex(7)
        state = {
            "reference_id": reference_id,
            "amount_paise": amount_paise,
            "currency": currency,
            "status": "created",
            "amount_paid_paise": 0,
            "payments": [],
            "short_url": f"https://rzp.example/mock/{link_id}",
        }
        cache.set(_link_key(link_id), state, timeout=_TTL)
        cache.set(_ref_key(reference_id), link_id, timeout=_TTL)
        return CreatedLink(link_id, state["short_url"], expires_at)

    def fetch_payment_link(self, link_id):
        state = cache.get(_link_key(link_id))
        if state is None:
            raise PermanentError("unknown link", code="link_not_found")
        return LinkState(
            link_id=link_id,
            reference_id=state["reference_id"],
            status=state["status"],
            amount_paise=state["amount_paise"],
            amount_paid_paise=state["amount_paid_paise"],
            currency=state["currency"],
            payments=[GatewayPayment(**p) for p in state["payments"]],
        )

    def cancel_payment_link(self, link_id):
        state = cache.get(_link_key(link_id))
        if state and state["status"] == "created":
            state["status"] = "cancelled"
            cache.set(_link_key(link_id), state, timeout=_TTL)

    def create_refund(self, payment_id, amount_paise, reference):
        return RefundResult("rfnd_mock_" + secrets.token_hex(7), "processed")

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        expected = hmac.new(MOCK_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")

"""Payment gateway interface (ARCHITECTURE §6.1, §9). The Razorpay adapter is added in Phase 6."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from apps.core.integrations import PermanentError, TransientError

__all__ = [
    "CreatedLink",
    "GatewayPayment",
    "LinkState",
    "PaymentGateway",
    "PermanentError",
    "RefundResult",
    "TransientError",
]


@dataclass(frozen=True)
class CreatedLink:
    link_id: str
    short_url: str
    expires_at: datetime | None


@dataclass(frozen=True)
class GatewayPayment:
    payment_id: str
    amount_paise: int
    currency: str
    status: str  # "captured", "authorized", "failed", "refunded"
    method: str = ""
    order_id: str = ""
    fee_paise: int = 0  # Razorpay's fee incl. tax; part of amount_paise when the customer bears the fee


@dataclass(frozen=True)
class LinkState:
    """Authoritative link state fetched from the gateway (never trust a webhook body alone)."""

    link_id: str
    reference_id: str
    status: str  # "created", "paid", "partially_paid", "expired", "cancelled"
    amount_paise: int
    amount_paid_paise: int
    currency: str
    payments: list[GatewayPayment] = field(default_factory=list)


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    status: str  # "pending", "processed", "failed"


class PaymentGateway(ABC):
    name: str

    @abstractmethod
    def create_payment_link(
        self,
        *,
        reference_id: str,
        amount_paise: int,
        currency: str,
        description: str,
        customer_phone: str,
        expires_at: datetime,
    ) -> CreatedLink:
        """Must be safe to retry with the same reference_id: return the existing link, never a second one."""

    @abstractmethod
    def fetch_payment_link(self, link_id: str) -> LinkState: ...

    @abstractmethod
    def cancel_payment_link(self, link_id: str) -> None: ...

    @abstractmethod
    def create_refund(self, payment_id: str, amount_paise: int, reference: str) -> RefundResult: ...

    @abstractmethod
    def find_refund(self, payment_id: str, reference: str) -> RefundResult | None:
        """Look up a refund we may already have created (by our reference) before creating another."""

    @abstractmethod
    def verify_webhook_signature(self, body: bytes, signature: str) -> bool: ...

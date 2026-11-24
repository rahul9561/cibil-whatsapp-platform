"""Credit report provider interface (ARCHITECTURE §9).

No real endpoint is assumed here. A concrete adapter for the authorised partner is written only from
that partner's documentation (Phase 10). Until then `MockCreditReportProvider` stands in.

Rules for every implementation:
- Never log or persist the OTP or applicant PII.
- Raise `TransientError` for retryable failures, `PermanentError` for everything else.
- `fetch_report` must be idempotent for a session: calling it again after a crash returns the same report
  (or the provider's status) and must not pull/bill a second report.
"""

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

from apps.core.integrations import PermanentError, TransientError

__all__ = [
    "Applicant",
    "CreditReportProvider",
    "OtpSession",
    "Prefill",
    "PermanentError",
    "ReportOutcome",
    "ReportResult",
    "TransientError",
    "VerifyOutcome",
]


@dataclass(frozen=True)
class Applicant:
    full_name: str
    pan: str
    dob: date | None  # not every provider needs it (see required_fields)
    mobile: str  # E.164 digits, e.g. 919876543210 (the customer's own WhatsApp number)
    email: str = ""
    pincode: str = ""
    gender: str = ""

    def __repr__(self) -> str:  # never leak PII through repr() in logs or tracebacks
        return "Applicant(<redacted>)"


@dataclass(frozen=True)
class Prefill:
    """Details looked up from the bureau by mobile number. Only what the report call needs is kept."""

    full_name: str = ""
    pan: str = ""
    gender: str = ""  # "M" / "F" / "" (unknown)
    ref: str = ""  # provider reference (e.g. Surepass client_id), not personal data

    def __repr__(self) -> str:
        return "Prefill(<redacted>)"


@dataclass(frozen=True)
class OtpSession:
    session_ref: str
    expires_at: datetime
    masked_destination: str = ""  # e.g. "******3210" so the bot can tell the customer where the OTP went


class VerifyOutcome(enum.StrEnum):
    VERIFIED = "verified"
    INVALID = "invalid"
    EXPIRED = "expired"
    LOCKED = "locked"  # provider blocked further attempts


class ReportOutcome(enum.StrEnum):
    SUCCESS = "success"
    NO_HIT = "no_hit"  # no credit history for this applicant


@dataclass(frozen=True)
class ReportResult:
    outcome: ReportOutcome
    report_ref: str = ""
    pdf_bytes: bytes = field(default=b"", repr=False)
    score: str = ""


class CreditReportProvider(ABC):
    name: str
    # True: the provider verifies the customer with an OTP (initiate_otp -> verify_otp -> fetch_report).
    # False: the provider returns the report on consent alone (fetch_report_without_otp). Ownership of the
    # PAN is then NOT verified by the bureau, so ReportService applies its PAN-misuse guard.
    requires_otp: bool = True
    # Customer details the bot must collect for this provider ("mobile" always comes from WhatsApp).
    required_fields: frozenset[str] = frozenset({"full_name", "pan", "dob"})

    # True: the provider can look up name/PAN/gender from the customer's mobile number (after consent).
    supports_prefill: bool = False

    def prefill_by_mobile(self, mobile: str, consent_ref: str) -> "Prefill | None":
        """Details linked to `mobile` (E.164 digits), or None if the bureau has no clear match."""
        raise NotImplementedError(f"{self.name} has no prefill lookup")

    def fetch_report_without_otp(self, applicant: Applicant, consent_ref: str) -> ReportResult:
        """Only for providers with requires_otp = False. Must be safe to call again for the same consent_ref
        (return the same report without a second pull) for as long as the provider allows."""
        raise NotImplementedError(f"{self.name} requires OTP verification")

    @abstractmethod
    def initiate_otp(self, applicant: Applicant, consent_ref: str) -> OtpSession: ...

    @abstractmethod
    def resend_otp(self, session_ref: str) -> OtpSession: ...

    @abstractmethod
    def verify_otp(self, session_ref: str, otp: str) -> VerifyOutcome: ...

    @abstractmethod
    def fetch_report(self, session_ref: str) -> ReportResult: ...

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
    dob: date
    mobile: str  # E.164 digits, e.g. 919876543210
    email: str = ""
    pincode: str = ""
    gender: str = ""

    def __repr__(self) -> str:  # never leak PII through repr() in logs or tracebacks
        return "Applicant(<redacted>)"


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

    @abstractmethod
    def initiate_otp(self, applicant: Applicant, consent_ref: str) -> OtpSession: ...

    @abstractmethod
    def resend_otp(self, session_ref: str) -> OtpSession: ...

    @abstractmethod
    def verify_otp(self, session_ref: str, otp: str) -> VerifyOutcome: ...

    @abstractmethod
    def fetch_report(self, session_ref: str) -> ReportResult: ...

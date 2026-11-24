"""Deterministic stand-in for the credit report provider (dev and tests only).

Behaviour is chosen by the applicant's PAN, so scenarios can be driven from WhatsApp in dev:

    PAN            scenario
    -------------  ---------------------------------------------------------------
    NOHIT0000N     report fetch returns NO_HIT
    FLAKY0000F     first 2 fetch calls raise TransientError, then success
    DOWNS0000D     every fetch raises TransientError
    REJEC0000R     initiate_otp raises PermanentError (provider rejects applicant)
    anything else  success

OTP: "123456" verifies, "000000" is expired, anything else is invalid; 5 wrong tries lock the session.
State lives in the Django cache so web and worker processes share it.
"""

import secrets
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from .base import (
    Applicant,
    CreditReportProvider,
    OtpSession,
    PermanentError,
    ReportOutcome,
    ReportResult,
    TransientError,
    VerifyOutcome,
)

VALID_OTP = "123456"
EXPIRED_OTP = "000000"
OTP_TTL = timedelta(minutes=10)
_STATE_TTL = 60 * 60 * 24


def mock_pdf(score: str) -> bytes:
    """A small, valid one-page PDF. Contains no applicant data."""
    text = f"MOCK CREDIT REPORT - score {score} - not a real report"
    stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class MockCreditReportProvider(CreditReportProvider):
    name = "mock"

    def _key(self, session_ref: str) -> str:
        return f"mockprovider:{session_ref}"

    def _get(self, session_ref: str) -> dict:
        state = cache.get(self._key(session_ref))
        if state is None:
            raise PermanentError("unknown session", code="session_not_found")
        return state

    def _put(self, session_ref: str, state: dict) -> None:
        cache.set(self._key(session_ref), state, timeout=_STATE_TTL)

    def initiate_otp(self, applicant: Applicant, consent_ref: str) -> OtpSession:
        if applicant.pan == "REJEC0000R":
            raise PermanentError("applicant rejected by provider", code="applicant_rejected")
        session_ref = "mock_" + secrets.token_hex(8)
        expires = timezone.now() + OTP_TTL
        self._put(
            session_ref,
            {"pan": applicant.pan, "verified": False, "bad": 0, "fetches": 0, "expires": expires.isoformat()},
        )
        return OtpSession(session_ref, expires, masked_destination="******" + applicant.mobile[-4:])

    def resend_otp(self, session_ref: str) -> OtpSession:
        state = self._get(session_ref)
        expires = timezone.now() + OTP_TTL
        state["expires"] = expires.isoformat()
        self._put(session_ref, state)
        return OtpSession(session_ref, expires)

    def verify_otp(self, session_ref: str, otp: str) -> VerifyOutcome:
        state = self._get(session_ref)
        if state["bad"] >= 5:
            return VerifyOutcome.LOCKED
        if otp == EXPIRED_OTP:
            return VerifyOutcome.EXPIRED
        if otp != VALID_OTP:
            state["bad"] += 1
            self._put(session_ref, state)
            return VerifyOutcome.LOCKED if state["bad"] >= 5 else VerifyOutcome.INVALID
        state["verified"] = True
        self._put(session_ref, state)
        return VerifyOutcome.VERIFIED

    def fetch_report(self, session_ref: str) -> ReportResult:
        state = self._get(session_ref)
        if not state["verified"]:
            raise PermanentError("session not verified", code="otp_not_verified")
        state["fetches"] += 1
        self._put(session_ref, state)
        pan = state["pan"]
        if pan == "DOWNS0000D" or (pan == "FLAKY0000F" and state["fetches"] <= 2):
            raise TransientError("provider timeout", code="provider_timeout")
        if pan == "NOHIT0000N":
            return ReportResult(ReportOutcome.NO_HIT)
        # Same report every time for a session: fetch is idempotent.
        return ReportResult(
            ReportOutcome.SUCCESS, report_ref=f"rpt_{session_ref}", pdf_bytes=mock_pdf("750"), score="750"
        )


class MockDirectCreditReportProvider(MockCreditReportProvider):
    """Behaves like Surepass: no OTP, needs name/PAN/gender. Same PAN scenarios as the OTP mock."""

    name = "mock_no_otp"
    requires_otp = False
    required_fields = frozenset({"full_name", "pan", "gender"})
    supports_prefill = True

    def prefill_by_mobile(self, mobile: str, consent_ref: str):
        """By the number's last digit: 9 -> no record, 8 -> provider down, otherwise RAHUL SHARMA,
        PAN "PFILL" + last 4 digits + "P", male."""
        from .base import Prefill

        digits = "".join(ch for ch in mobile if ch.isdigit())
        if not consent_ref:
            raise PermanentError("consent is required", code="consent_missing")
        if digits.endswith("9"):
            return None
        if digits.endswith("8"):
            raise TransientError("prefill timeout", code="provider_timeout")
        return Prefill(full_name="RAHUL SHARMA", pan=f"PFILL{digits[-4:]}P", gender="M", ref="prefill_mock")

    def fetch_report_without_otp(self, applicant: Applicant, consent_ref: str) -> ReportResult:
        if applicant.pan == "REJEC0000R":
            raise PermanentError("applicant rejected by provider", code="applicant_rejected")
        session_ref = f"direct_{consent_ref}"
        state = cache.get(self._key(session_ref)) or {
            "pan": applicant.pan,
            "verified": True,
            "bad": 0,
            "fetches": 0,
            "expires": "",
        }
        self._put(session_ref, state)
        return self.fetch_report(session_ref)

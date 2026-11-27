"""SmartAuth ("bureau B") CIBIL credit report adapter: bureau data in, our own PDF out.

Built strictly from the client code supplied by the business (2026-11-25) and the response sample in the
befisc_reportforge repository (input/sample.json):

    POST {BUREAU_B_BASE_URL}/TILJ
    authkey: <BUREAU_B_AUTHKEY>
    {"name": "...", "mobile": "...", "pan": "...", "consent_text": "<fixed text>", "consent": "Y"}

    200 {"status": 1, "txn_id": "...", "message": "...", "billable": true,
         "result": {"htmlUrl": "...", "pdf_report_url": "...",
                    "cibilData": {"GetCustomerAssetsResponse": {"GetCustomerAssetsSuccess": {"Asset":
                        {"TrueLinkCreditReport": {...}}}}}}}
    anything else: {"status": <not 1>, "message": "..."}

The PDF is generated here from `cibilData` by befisc_reportforge (the business's own report design,
installed from its GitHub repository at a pinned commit). Nothing is downloaded from the links in the
response, so there is no third-party page to trust or convert.

Like Surepass this endpoint has NO OTP (requires_otp = False), so the PAN-misuse guard applies. SmartAuth
has no lookup by mobile number: that is a separate provider (PREFILL_PROVIDER=surepass), whose details
(after the customer confirms them) are what this adapter sends. Without one the bot asks name and PAN.
Things the sample does not show are marked CONFIRM below.

Never log the request or response: both carry name, mobile, PAN and the whole credit history.
"""

import json
import logging
import re

import requests
from django.conf import settings
from django.core.cache import cache

from apps.core.crypto import decrypt_str, encrypt_str

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

logger = logging.getLogger(__name__)

REPORT_PATH = "/TILJ"
# Sent with every request, exactly as in the supplied client.
CONSENT_TEXT = (
    "We confirm obtaining valid customer consent to access/process their mobile/pan/name data. "
    "Consent remains valid, informed, and unwithdrawn."
)
API_TIMEOUT = (10, 70)  # connect, read (the supplied client waits 70 s)
MAX_PDF_BYTES = 15 * 1024 * 1024
# A retry within this time re-renders the cached response instead of pulling (and paying for) a second
# report. The response is kept encrypted in Redis only for this long.
RESULT_CACHE_SECONDS = 15 * 60
_TRUELINK = (
    "cibilData",
    "GetCustomerAssetsResponse",
    "GetCustomerAssetsSuccess",
    "Asset",
    "TrueLinkCreditReport",
)
SCORE_PATHS = (
    _TRUELINK + ("CreditScore", "riskScore"),  # as read by the supplied client
    _TRUELINK + ("Borrower", "CreditScore", "riskScore"),  # as read by befisc_reportforge
)


def _code(text) -> str:
    """Provider messages go into our failure_code field: keep them short and harmless."""
    return re.sub(r"[^a-z0-9_]+", "_", str(text or "unknown").lower())[:40].strip("_") or "unknown"


def _dig(data, path):
    for key in path:
        if isinstance(data, list):  # the bureau sometimes sends a one-item list where an object is expected
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


_engine = None


def report_engine():
    """The PDF engine, created once per process (it is thread-safe and holds only configuration).

    Checked BEFORE the paid API call: if the package is missing, the report could not be delivered."""
    global _engine
    if _engine is None:
        try:
            from befisc_reportforge import PDFEngine

            _engine = PDFEngine()
        except Exception as exc:  # ImportError, AssetError (fonts), ...
            logger.error(
                "smartauth: befisc_reportforge not usable - rebuild the backend image (%s)",
                type(exc).__name__,
            )
            raise PermanentError("report PDF engine not installed", code="pdf_engine_missing") from exc
    return _engine


def render_pdf(body: dict) -> bytes:
    from befisc_reportforge import ValidationError

    try:
        pdf = report_engine().render_bytes(body)
    except ValidationError as exc:
        # status 1 but no TrueLinkCreditReport. CONFIRM with SmartAuth: is this how "no credit history"
        # looks? Then it should become NO_HIT. Either way the customer is told they weren't charged.
        raise PermanentError("smartauth response has no credit report", code="report_missing") from exc
    except Exception as exc:  # ReportForgeError or anything else: fail this request, not the worker
        logger.error("smartauth: PDF generation failed (%s)", type(exc).__name__)
        raise PermanentError("report PDF could not be generated", code="report_unreadable") from exc
    if not pdf.startswith(b"%PDF-") or len(pdf) > MAX_PDF_BYTES:
        raise PermanentError("generated report is not a valid PDF", code="report_unreadable")
    return pdf


class SmartAuthCreditReportProvider(CreditReportProvider):
    name = "smartauth"
    requires_otp = False
    required_fields = frozenset({"full_name", "pan"})  # mobile always comes from WhatsApp

    def __init__(self, session: requests.Session | None = None):
        # No automatic HTTP retries: a repeated POST could pull (and bill) a second report.
        self.session = session or requests.Session()
        self.base_url = settings.BUREAU_B_BASE_URL.rstrip("/")
        self.authkey = settings.BUREAU_B_AUTHKEY

    # -- OTP: not offered by this endpoint -----------------------------------------------------------------

    def initiate_otp(self, applicant: Applicant, consent_ref: str) -> OtpSession:
        raise PermanentError("smartauth report endpoint has no OTP step", code="otp_not_supported")

    def resend_otp(self, session_ref: str) -> OtpSession:
        raise PermanentError("smartauth report endpoint has no OTP step", code="otp_not_supported")

    def verify_otp(self, session_ref: str, otp: str) -> VerifyOutcome:
        raise PermanentError("smartauth report endpoint has no OTP step", code="otp_not_supported")

    def fetch_report(self, session_ref: str) -> ReportResult:
        raise PermanentError("use fetch_report_without_otp", code="otp_not_supported")

    # -- report ---------------------------------------------------------------------------------------------

    def fetch_report_without_otp(self, applicant: Applicant, consent_ref: str) -> ReportResult:
        if not consent_ref:
            raise PermanentError("consent is required before calling SmartAuth", code="consent_missing")
        if not self.base_url or not self.authkey:
            raise PermanentError(
                "BUREAU_B_BASE_URL / BUREAU_B_AUTHKEY not set", code="provider_not_configured"
            )
        report_engine()  # without it a paid report could not be delivered: check before paying for it
        cache_key = f"smartauth:result:{consent_ref}"
        cached = cache.get(cache_key)
        if cached:
            body = json.loads(decrypt_str(cached))
        else:
            body = self._call(self._payload(applicant))
            cache.set(cache_key, encrypt_str(json.dumps(body)), timeout=RESULT_CACHE_SECONDS)
        result = body.get("result") or {}
        score = next((s for s in (_dig(result, p) for p in SCORE_PATHS) if s not in (None, "")), "")
        return ReportResult(
            ReportOutcome.SUCCESS,
            report_ref=str(body.get("txn_id") or ""),
            pdf_bytes=render_pdf(body),
            score=str(score),
        )

    def _payload(self, applicant: Applicant) -> dict:
        digits = re.sub(r"\D", "", applicant.mobile or "")
        if len(digits) != 12 or not digits.startswith("91"):
            raise PermanentError("an Indian mobile number is required", code="mobile_not_indian")
        if not applicant.full_name or not applicant.pan:
            raise PermanentError("name and PAN are required", code="applicant_incomplete")
        return {
            "name": applicant.full_name,
            "mobile": digits[2:],  # CONFIRM with SmartAuth: 10 digits (as Surepass) rather than 91XXXXXXXXXX
            "pan": applicant.pan,
            "consent_text": CONSENT_TEXT,
            "consent": "Y",
        }

    def _call(self, payload: dict) -> dict:
        try:
            resp = self.session.post(
                self.base_url + REPORT_PATH,
                json=payload,
                headers={"authkey": self.authkey, "Content-Type": "application/json"},
                timeout=API_TIMEOUT,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # CONFIRM with SmartAuth: whether a timed-out call is billed and how to look it up (txn_id).
            raise TransientError("smartauth: network error", code="provider_timeout") from exc
        if resp.status_code in (401, 403):
            logger.error(
                "smartauth: authentication failed (status %s) - check BUREAU_B_AUTHKEY", resp.status_code
            )
            raise PermanentError("smartauth: authentication failed", code="provider_auth")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError("smartauth: temporary error", code=f"provider_http_{resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            body = None  # e.g. an HTML error page from a proxy
        if resp.status_code != 200 or not isinstance(body, dict):
            raise PermanentError("smartauth: unexpected response", code=f"smartauth_http_{resp.status_code}")
        if body.get("status") != 1:
            # CONFIRM with SmartAuth: the message for "no credit history" (no hit), so it can map to NO_HIT
            # (customer told they weren't charged) instead of FAILED. Both cost the customer nothing.
            raise PermanentError(
                "smartauth: request rejected", code=f"smartauth_{_code(body.get('message'))}"
            )
        return body

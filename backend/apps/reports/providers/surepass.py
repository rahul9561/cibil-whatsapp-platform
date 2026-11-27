"""Surepass CIBIL credit report adapter.

Built strictly from the request/response sample supplied by the business (2026-11-24):

    POST {SUREPASS_BASE_URL}/production/api/v1/credit-report-cibil/fetch-report-pdf
    Authorization: Bearer <SUREPASS_API_TOKEN>
    {"mobile": "9988776655", "pan": "EKRPR1234F", "name": "...", "gender": "male", "consent": "Y"}

    200 {"data": {"client_id": "...", "credit_score": "750", "credit_report_link": "<S3 URL, 10 min>",
                  "credit_report_base64": null, ...},
         "success": true, "status_code": 200, "message_code": "success"}

This endpoint has NO OTP: Surepass returns the report on consent alone (requires_otp = False). Things the
sample does not show and that must be confirmed with Surepass before go-live are marked CONFIRM below.

Never log the request or response: both carry name, mobile and PAN; the PDF link is a bearer URL.
"""

import base64
import binascii
import logging
import re
import time
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache

from .base import (
    Applicant,
    CreditReportProvider,
    OtpSession,
    PermanentError,
    Prefill,
    ReportOutcome,
    ReportResult,
    TransientError,
    VerifyOutcome,
)

logger = logging.getLogger(__name__)

FETCH_PATH = "/production/api/v1/credit-report-cibil/fetch-report-pdf"
# Prefill-by-mobile lives on a different Surepass host (sample supplied 2026-11-24).
PREFILL_PATH = "/api/v1/prefill/prefill-by-mobile"
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
API_TIMEOUT = (10, 120)  # connect, read: the bureau call can take a while
PDF_TIMEOUT = (10, 60)
MAX_PDF_BYTES = 15 * 1024 * 1024
LINK_CACHE_SECONDS = 8 * 60  # the sample's link is valid for 600 s: keep a margin
# CONFIRM with Surepass: accepted gender values. The sample only shows "male".
GENDERS = {"M": "male", "F": "female"}


def _code(text) -> str:
    """Provider message codes go into our failure_code field: keep them short and harmless."""
    return re.sub(r"[^a-z0-9_]+", "_", str(text or "unknown").lower())[:40].strip("_") or "unknown"


class SurepassCreditReportProvider(CreditReportProvider):
    name = "surepass"
    requires_otp = False
    required_fields = frozenset({"full_name", "pan", "gender"})
    supports_prefill = True

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.base_url = settings.SUREPASS_BASE_URL.rstrip("/")
        self.token = settings.SUREPASS_API_TOKEN
        self.pdf_hosts = tuple(h.strip().lower() for h in settings.SUREPASS_PDF_HOSTS if h.strip())
        self.kyc_base_url = settings.SUREPASS_KYC_BASE_URL.rstrip("/")
        self.prefill_token = settings.SUREPASS_PREFILL_TOKEN or self.token

    # -- prefill by mobile ----------------------------------------------------------------------------------

    def prefill_by_mobile(self, mobile: str, consent_ref: str) -> Prefill | None:
        """Look up name/PAN/gender for the customer's own number. Everything else in the response
        (addresses, e-mail, other IDs, phone history) is discarded here and never stored or logged."""
        if not consent_ref:
            raise PermanentError("consent is required before a bureau lookup", code="consent_missing")
        digits = re.sub(r"\D", "", mobile or "")
        if len(digits) != 12 or not digits.startswith("91"):
            raise PermanentError("an Indian mobile number is required", code="mobile_not_indian")
        try:
            resp = self.session.post(
                self.kyc_base_url + PREFILL_PATH,
                json={"mobile": digits[2:]},
                headers={"Authorization": f"Bearer {self.prefill_token}", "Content-Type": "application/json"},
                timeout=API_TIMEOUT,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            raise TransientError("surepass prefill: network error", code="provider_timeout") from exc
        if resp.status_code in (401, 403):
            logger.error("surepass prefill: authentication failed (status %s)", resp.status_code)
            raise PermanentError("surepass prefill: authentication failed", code="provider_auth")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(
                "surepass prefill: temporary error", code=f"provider_http_{resp.status_code}"
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or not body.get("success"):
            return None  # no record for this number (CONFIRM with Surepass: exact "not found" response)

        data = body.get("data") or {}
        # The live API (2026-11-27) nests everything under data.details; the earlier sample had it directly
        # under data. Read whichever is present.
        details = data.get("details") if isinstance(data.get("details"), dict) else data
        personal = details.get("personal_info") or {}
        name = " ".join(
            str(personal.get("full_name") or details.get("name") or data.get("name") or "").split()
        )
        gender = {"male": "M", "female": "F"}.get(str(personal.get("gender") or "").strip().lower(), "")
        pans = {
            str(item.get("id_number") or "").strip().upper()
            for item in ((details.get("identity_info") or {}).get("pan_number") or [])
            if isinstance(item, dict)
        }
        pans = {p for p in pans if PAN_RE.match(p)}
        pan = pans.pop() if len(pans) == 1 else ""  # none or several PANs: the customer types it
        if not name and not pan:
            return None
        return Prefill(full_name=name, pan=pan, gender=gender, ref=str(data.get("client_id") or ""))

    # -- OTP: not offered by this endpoint -----------------------------------------------------------------

    def initiate_otp(self, applicant: Applicant, consent_ref: str) -> OtpSession:
        raise PermanentError("surepass report endpoint has no OTP step", code="otp_not_supported")

    def resend_otp(self, session_ref: str) -> OtpSession:
        raise PermanentError("surepass report endpoint has no OTP step", code="otp_not_supported")

    def verify_otp(self, session_ref: str, otp: str) -> VerifyOutcome:
        raise PermanentError("surepass report endpoint has no OTP step", code="otp_not_supported")

    def fetch_report(self, session_ref: str) -> ReportResult:
        raise PermanentError("use fetch_report_without_otp", code="otp_not_supported")

    # -- report ---------------------------------------------------------------------------------------------

    def fetch_report_without_otp(self, applicant: Applicant, consent_ref: str) -> ReportResult:
        if not consent_ref:
            raise PermanentError("consent is required before calling Surepass", code="consent_missing")
        cache_key = f"surepass:result:{consent_ref}"
        cached = cache.get(cache_key)
        if cached:
            # A retry after the API already answered: re-download instead of pulling (and paying) again.
            return self._result(cached, download_only=True)

        body = self._call(self._payload(applicant))
        data = body.get("data") or {}
        summary = {
            "client_id": str(data.get("client_id") or ""),
            "score": str(data.get("credit_score") or ""),
            "link": data.get("credit_report_link") or "",
            "b64": data.get("credit_report_base64") or "",
        }
        if summary["link"] or summary["b64"]:
            cache.set(cache_key, summary, timeout=LINK_CACHE_SECONDS)
        return self._result(summary)

    def _payload(self, applicant: Applicant) -> dict:
        digits = re.sub(r"\D", "", applicant.mobile or "")
        if len(digits) != 12 or not digits.startswith("91"):
            raise PermanentError("an Indian mobile number is required", code="mobile_not_indian")
        gender = GENDERS.get(applicant.gender)
        if not gender:
            raise PermanentError("gender not supported by the provider", code="gender_not_supported")
        if not applicant.full_name or not applicant.pan:
            raise PermanentError("name and PAN are required", code="applicant_incomplete")
        return {
            "mobile": digits[2:],
            "pan": applicant.pan,
            "name": applicant.full_name,
            "gender": gender,
            "consent": "Y",
        }

    def _call(self, payload: dict) -> dict:
        try:
            resp = self.session.post(
                self.base_url + FETCH_PATH,
                json=payload,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                timeout=API_TIMEOUT,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # CONFIRM with Surepass: whether a timed-out call is billed and how to look it up afterwards.
            raise TransientError("surepass: network error", code="provider_timeout") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code in (401, 403):
            logger.error(
                "surepass: authentication failed (status %s) - check SUREPASS_API_TOKEN", resp.status_code
            )
            raise PermanentError("surepass: authentication failed", code="provider_auth")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError("surepass: temporary error", code=f"provider_http_{resp.status_code}")
        if resp.status_code >= 400 or not body.get("success"):
            # CONFIRM with Surepass: the message_code returned when the bureau has no record for the PAN
            # ("no hit"), so it maps to NO_HIT (customer told they weren't charged) instead of FAILED.
            raise PermanentError(
                "surepass: request rejected",
                code=f"surepass_{_code(body.get('message_code') or resp.status_code)}",
            )
        return body

    def _result(self, summary: dict, *, download_only: bool = False) -> ReportResult:
        if summary.get("b64"):
            pdf = self._decode(summary["b64"])
        elif summary.get("link"):
            pdf = self._download(summary["link"])
        else:
            raise PermanentError("surepass returned no report document", code="report_missing")
        return ReportResult(
            ReportOutcome.SUCCESS,
            report_ref=summary.get("client_id", ""),
            pdf_bytes=pdf,
            score=summary.get("score", ""),
        )

    @staticmethod
    def _decode(b64: str) -> bytes:
        try:
            pdf = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PermanentError(
                "surepass returned an unreadable document", code="report_unreadable"
            ) from exc
        if not pdf.startswith(b"%PDF-") or len(pdf) > MAX_PDF_BYTES:
            raise PermanentError("surepass document is not a valid PDF", code="report_unreadable")
        return pdf

    def _download(self, link: str) -> bytes:
        url = urlparse(link)
        host = (url.hostname or "").lower()
        if url.scheme != "https" or not any(host == h or host.endswith("." + h) for h in self.pdf_hosts):
            # Only fetch from the provider's document store: never follow an arbitrary URL (SSRF).
            raise PermanentError("report link points to an unexpected host", code="report_link_untrusted")
        last: Exception | None = None
        for attempt in range(3):  # the link is short-lived: retry quickly here rather than via Celery
            try:
                with self.session.get(link, timeout=PDF_TIMEOUT, stream=True, allow_redirects=False) as resp:
                    if resp.status_code in (403, 404):
                        raise PermanentError("report link expired or invalid", code="report_link_expired")
                    if resp.status_code != 200:
                        raise TransientError("report download failed", code="report_download_failed")
                    chunks, size = [], 0
                    for chunk in resp.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > MAX_PDF_BYTES:
                            raise PermanentError("report file too large", code="report_too_large")
                        chunks.append(chunk)
                pdf = b"".join(chunks)
                if not pdf.startswith(b"%PDF-"):
                    raise PermanentError("downloaded file is not a PDF", code="report_unreadable")
                return pdf
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, TransientError) as exc:
                last = exc
                time.sleep(1 + attempt)
        raise TransientError("report download failed", code="report_download_failed") from last

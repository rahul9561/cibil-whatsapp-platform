"""SmartAuth ("bureau B") adapter: request format, PDF from bureau data (befisc_reportforge), errors,
retries and the full WhatsApp journey. The HTTP layer is faked; the PDF engine really runs."""

import json

import pytest
import requests

from apps.conversations.models import Conversation
from apps.core.crypto import decrypt_str
from apps.core.integrations import PermanentError, TransientError
from apps.customers.models import Customer
from apps.messaging.dev import simulate_inbound
from apps.reports.models import ReportRequest, ReportStatus
from apps.reports.providers import smartauth
from apps.reports.providers.base import Applicant, ReportOutcome
from apps.reports.providers.smartauth import CONSENT_TEXT, SmartAuthCreditReportProvider

BASE = "https://api.smartauth.example/v1"


def ok_body(score="765", truelink=True):
    """Shape of the real response (befisc_reportforge input/sample.json), with made-up data."""
    report = {
        "Borrower": {
            "BorrowerName": {"Name": {"Forename": "RAHUL", "Surname": "SHARMA"}},
            "CreditScore": {"riskScore": score} if score is not None else {},
        }
    }
    asset = {"TrueLinkCreditReport": report} if truelink else {}
    return {
        "status": 1,
        "txn_id": "TXN-42",
        "message": "Success",
        "billable": True,
        "result": {
            "htmlUrl": "https://elsewhere.example/r.html",
            "pdf_report_url": "https://elsewhere.example/r.pdf",
            "cibilData": {"GetCustomerAssetsResponse": {"GetCustomerAssetsSuccess": {"Asset": asset}}},
        },
    }


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, post=None):
        self.posts, self.gets = [], []
        self._post = post or FakeResponse(200, ok_body())

    def post(self, url, **kw):
        self.posts.append((url, kw))
        if isinstance(self._post, Exception):
            raise self._post
        return self._post

    def get(self, url, **kw):  # nothing in the response may ever be downloaded
        self.gets.append(url)
        raise AssertionError(f"unexpected download: {url}")


@pytest.fixture(autouse=True)
def configured(settings):
    settings.BUREAU_B_BASE_URL = BASE
    settings.BUREAU_B_AUTHKEY = "test-authkey"
    settings.SUREPASS_API_TOKEN = ""
    settings.SUREPASS_PREFILL_TOKEN = ""


def applicant(**kw):
    data = {"full_name": "RAHUL SHARMA", "pan": "ABCDE1234F", "dob": None, "mobile": "919876543210"}
    return Applicant(**{**data, **kw})


# --- happy path ------------------------------------------------------------------------------------------


def test_request_matches_the_supplied_client_and_the_pdf_is_generated_here():
    session = FakeSession()
    result = SmartAuthCreditReportProvider(session=session).fetch_report_without_otp(applicant(), "consent-1")

    ((url, kw),) = session.posts
    assert url == f"{BASE}/TILJ"
    assert kw["headers"] == {"authkey": "test-authkey", "Content-Type": "application/json"}
    assert kw["json"] == {
        "name": "RAHUL SHARMA",
        "mobile": "9876543210",
        "pan": "ABCDE1234F",
        "consent_text": CONSENT_TEXT,
        "consent": "Y",
    }
    assert result.outcome == ReportOutcome.SUCCESS and result.report_ref == "TXN-42" and result.score == "765"
    assert result.pdf_bytes.startswith(b"%PDF-") and len(result.pdf_bytes) > 2000
    assert session.gets == []  # htmlUrl / pdf_report_url are never fetched


def test_score_from_the_clients_path_too():
    body = ok_body(score=None)
    body["result"]["cibilData"]["GetCustomerAssetsResponse"]["GetCustomerAssetsSuccess"]["Asset"][
        "TrueLinkCreditReport"
    ]["CreditScore"] = {"riskScore": "701"}
    session = FakeSession(post=FakeResponse(200, body))
    assert (
        SmartAuthCreditReportProvider(session=session).fetch_report_without_otp(applicant(), "c-s").score
        == "701"
    )


def test_missing_score_is_not_an_error():
    session = FakeSession(post=FakeResponse(200, ok_body(score=None)))
    result = SmartAuthCreditReportProvider(session=session).fetch_report_without_otp(applicant(), "c-2")
    assert result.score == "" and result.pdf_bytes.startswith(b"%PDF-")


def test_a_retry_renders_again_instead_of_paying_for_a_second_report():
    from django.core.cache import cache

    session = FakeSession()
    provider = SmartAuthCreditReportProvider(session=session)
    first = provider.fetch_report_without_otp(applicant(), "c-3")
    second = provider.fetch_report_without_otp(applicant(), "c-3")
    assert len(session.posts) == 1 and second.pdf_bytes.startswith(b"%PDF-") and second.score == first.score
    stored = cache.get("smartauth:result:c-3")
    assert (
        "RAHUL" not in stored and json.loads(decrypt_str(stored))["txn_id"] == "TXN-42"
    )  # encrypted at rest


def test_asks_only_name_and_pan():
    p = SmartAuthCreditReportProvider(session=FakeSession())
    assert p.required_fields == frozenset({"full_name", "pan"}) and p.requires_otp is False
    assert p.supports_prefill is False  # prefill is a separate provider (PREFILL_PROVIDER)


# --- failures --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        (
            FakeResponse(200, {"status": 0, "message": "Record Not Found"}),
            PermanentError,
            "smartauth_record_not_found",
        ),
        (FakeResponse(401, {"message": "bad key"}), PermanentError, "provider_auth"),
        (FakeResponse(503, None), TransientError, "provider_http_503"),
        (FakeResponse(200, None), PermanentError, "smartauth_http_200"),
        (FakeResponse(200, ok_body(truelink=False)), PermanentError, "report_missing"),
        (requests.exceptions.ReadTimeout(), TransientError, "provider_timeout"),
    ],
)
def test_api_errors_map_to_retryable_or_final(response, error, code):
    with pytest.raises(error) as exc:
        SmartAuthCreditReportProvider(session=FakeSession(post=response)).fetch_report_without_otp(
            applicant(), "c"
        )
    assert exc.value.code == code


def test_bad_input_is_refused_before_calling_the_api():
    session = FakeSession()
    with pytest.raises(PermanentError) as exc:
        SmartAuthCreditReportProvider(session=session).fetch_report_without_otp(
            applicant(mobile="12345"), "c"
        )
    assert exc.value.code == "mobile_not_indian" and session.posts == []


def test_no_paid_call_when_the_pdf_engine_is_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_engine(name, *a, **kw):
        if name == "befisc_reportforge":
            raise ImportError("No module named 'befisc_reportforge'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(smartauth, "_engine", None)
    monkeypatch.setattr(builtins, "__import__", no_engine)
    session = FakeSession()
    with pytest.raises(PermanentError) as exc:
        SmartAuthCreditReportProvider(session=session).fetch_report_without_otp(applicant(), "c-6")
    assert exc.value.code == "pdf_engine_missing" and session.posts == []


# --- whole journey through the bot ------------------------------------------------------------------------


@pytest.mark.django_db
def test_whatsapp_journey_with_smartauth(settings, monkeypatch):
    settings.CREDIT_PROVIDER = "smartauth"
    settings.PREFILL_BY_MOBILE = True  # no Surepass token: falls back to asking name and PAN
    session = FakeSession()
    monkeypatch.setattr(smartauth.requests, "Session", lambda: session)
    wa = "919800044401"

    def say(text=None, button=None):
        return " || ".join(r.get("body", "") for r in simulate_inbound(wa, text=text, reply_id=button))

    assert "full name" in say("Hi")
    assert "PAN" in say("Rahul Sharma")
    shown = say("ABCDE1234F")
    assert "check your details" in shown and "Gender" not in shown  # no gender question for SmartAuth
    assert "Do you agree?" in say(button="confirm")
    assert "fetching your credit report" in say(button="agree")

    req = ReportRequest.objects.get()
    assert req.report_status == ReportStatus.GENERATED and req.provider_name == "smartauth"
    assert req.payment_links.count() == 1
    assert session.posts[0][1]["json"]["mobile"] == "9800044401"
    customer = Customer.objects.by_wa_id(wa).get()
    assert Conversation.objects.get(customer=customer).state == "awaiting_payment"

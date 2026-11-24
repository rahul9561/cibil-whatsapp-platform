"""Surepass adapter against a fake HTTP session, using the request/response shapes from the sample."""

import base64
import json
from datetime import date
from unittest import mock

import pytest
import requests

from apps.core.integrations import PermanentError, TransientError
from apps.reports.providers.base import Applicant, ReportOutcome
from apps.reports.providers.surepass import FETCH_PATH, SurepassCreditReportProvider

PDF = b"%PDF-1.4 surepass sample report"
LINK = (
    "https://aadhaar-kyc-docs.s3.amazonaws.com/user123/credit_report_cibil/x/credit_report_1.pdf"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=600&X-Amz-Signature=abc"
)


def sample_body(**data_overrides):
    data = {
        "client_id": "credit_report_cibil_pdf_xfOSfdDRgierjgNdZelb",
        "name": "VISHAL RATHORE",
        "mobile": "9988776655",
        "pan": "EKRPR1234F",
        "gender": "male",
        "user_email": None,
        "credit_score": "750",
        "credit_report": None,
        "credit_report_link": LINK,
        "credit_report_base64": None,
    }
    data.update(data_overrides)
    return {
        "data": data,
        "status_code": 200,
        "success": True,
        "message": "Success",
        "message_code": "success",
    }


def applicant(**kw):
    base = {
        "full_name": "Vishal Rathore",
        "pan": "EKRPR1234F",
        "dob": None,
        "mobile": "919988776655",
        "gender": "M",
    }
    return Applicant(**{**base, **kw})


class FakeResponse:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self._body = body
        self._content = content

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, size):
        for i in range(0, len(self._content), size):
            yield self._content[i : i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_session(post=None, get=None):
    session = mock.Mock()
    session.post.side_effect = post if callable(post) or isinstance(post, Exception) else None
    if not session.post.side_effect:
        session.post.return_value = post or FakeResponse(body=sample_body())
    if isinstance(get, Exception) or callable(get):
        session.get.side_effect = get
    else:
        session.get.return_value = get or FakeResponse(content=PDF)
    return session


@pytest.fixture(autouse=True)
def surepass_settings(settings, monkeypatch):
    settings.SUREPASS_API_TOKEN = "test-token"
    settings.SUREPASS_BASE_URL = "https://app.surepass.app"
    settings.SUREPASS_PDF_HOSTS = ["amazonaws.com"]
    monkeypatch.setattr("apps.reports.providers.surepass.time.sleep", lambda s: None)


def test_request_matches_the_documented_call_and_pdf_is_downloaded():
    s = fake_session()
    result = SurepassCreditReportProvider(session=s).fetch_report_without_otp(applicant(), consent_ref="c-1")

    url = s.post.call_args[0][0]
    kwargs = s.post.call_args[1]
    assert url == "https://app.surepass.app" + FETCH_PATH
    assert url.endswith("/production/api/v1/credit-report-cibil/fetch-report-pdf")
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["json"] == {
        "mobile": "9988776655",
        "pan": "EKRPR1234F",
        "name": "Vishal Rathore",
        "gender": "male",
        "consent": "Y",
    }
    assert kwargs["timeout"]

    assert s.get.call_args[0][0] == LINK
    assert s.get.call_args[1]["allow_redirects"] is False
    assert result.outcome == ReportOutcome.SUCCESS
    assert result.pdf_bytes == PDF
    assert result.score == "750"
    assert result.report_ref == "credit_report_cibil_pdf_xfOSfdDRgierjgNdZelb"


def test_base64_document_is_used_when_provided():
    body = sample_body(credit_report_link=None, credit_report_base64=base64.b64encode(PDF).decode())
    s = fake_session(post=FakeResponse(body=body))
    result = SurepassCreditReportProvider(session=s).fetch_report_without_otp(applicant(), consent_ref="c-2")
    assert result.pdf_bytes == PDF
    s.get.assert_not_called()


def test_retry_redownloads_without_calling_surepass_again():
    attempts = {"n": 0}

    def flaky_get(*a, **k):
        attempts["n"] += 1
        if attempts["n"] <= 3:  # first adapter call: all three quick download retries fail
            raise requests.exceptions.ConnectionError()
        return FakeResponse(content=PDF)

    s = fake_session(get=flaky_get)
    provider = SurepassCreditReportProvider(session=s)
    with pytest.raises(TransientError) as exc:
        provider.fetch_report_without_otp(applicant(), consent_ref="c-3")
    assert exc.value.code == "report_download_failed"
    result = provider.fetch_report_without_otp(applicant(), consent_ref="c-3")  # e.g. the Celery retry
    assert result.pdf_bytes == PDF
    assert s.post.call_count == 1  # charged once


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example.com/report.pdf",
        "http://aadhaar-kyc-docs.s3.amazonaws.com/report.pdf",
        "https://amazonaws.com.evil.example/report.pdf",
    ],
)
def test_untrusted_links_are_never_fetched(link):
    s = fake_session(post=FakeResponse(body=sample_body(credit_report_link=link)))
    with pytest.raises(PermanentError) as exc:
        SurepassCreditReportProvider(session=s).fetch_report_without_otp(applicant(), consent_ref="c-4")
    assert exc.value.code == "report_link_untrusted"
    s.get.assert_not_called()


@pytest.mark.parametrize(
    ("get", "code"),
    [
        (FakeResponse(status=403, content=b"AccessDenied"), "report_link_expired"),
        (FakeResponse(content=b"<html>not a pdf</html>"), "report_unreadable"),
    ],
)
def test_bad_downloads(get, code):
    s = fake_session(get=get)
    with pytest.raises(PermanentError) as exc:
        SurepassCreditReportProvider(session=s).fetch_report_without_otp(applicant(), consent_ref="c-5")
    assert exc.value.code == code


@pytest.mark.parametrize(
    ("post", "kind", "code"),
    [
        (FakeResponse(status=401, body={"message": "unauthorized"}), PermanentError, "provider_auth"),
        (FakeResponse(status=500, body={}), TransientError, "provider_http_500"),
        (FakeResponse(status=429, body={}), TransientError, "provider_http_429"),
        (requests.exceptions.ReadTimeout(), TransientError, "provider_timeout"),
        (
            FakeResponse(status=422, body={"success": False, "message_code": "Invalid PAN!"}),
            PermanentError,
            "surepass_invalid_pan",
        ),
        (
            FakeResponse(status=200, body={"success": False, "message_code": "verification_failed"}),
            PermanentError,
            "surepass_verification_failed",
        ),
    ],
)
def test_error_mapping(post, kind, code):
    s = fake_session(post=post)
    with pytest.raises(kind) as exc:
        SurepassCreditReportProvider(session=s).fetch_report_without_otp(applicant(), consent_ref="c-6")
    assert exc.value.code == code


def test_success_without_document_is_an_error():
    s = fake_session(post=FakeResponse(body=sample_body(credit_report_link=None)))
    with pytest.raises(PermanentError) as exc:
        SurepassCreditReportProvider(session=s).fetch_report_without_otp(applicant(), consent_ref="c-7")
    assert exc.value.code == "report_missing"


@pytest.mark.parametrize(
    ("kw", "consent", "code"),
    [
        ({}, "", "consent_missing"),
        ({"mobile": "14155550100"}, "c", "mobile_not_indian"),
        ({"gender": "O"}, "c", "gender_not_supported"),
        ({"full_name": ""}, "c", "applicant_incomplete"),
    ],
)
def test_input_validation_happens_before_any_call(kw, consent, code):
    s = fake_session()
    with pytest.raises(PermanentError) as exc:
        SurepassCreditReportProvider(session=s).fetch_report_without_otp(applicant(**kw), consent_ref=consent)
    assert exc.value.code == code
    s.post.assert_not_called()


def test_otp_methods_are_refused():
    p = SurepassCreditReportProvider(session=fake_session())
    assert p.requires_otp is False and p.required_fields == {"full_name", "pan", "gender"}
    with pytest.raises(PermanentError):
        p.initiate_otp(applicant(dob=date(1990, 1, 1)), "c")


def test_responses_are_never_logged(caplog):
    caplog.set_level("DEBUG")
    s = fake_session()
    SurepassCreditReportProvider(session=s).fetch_report_without_otp(applicant(), consent_ref="c-8")
    logged = json.dumps([r.getMessage() for r in caplog.records])
    for secret in ("EKRPR1234F", "9988776655", "VISHAL", "X-Amz-Signature", "test-token"):
        assert secret not in logged


def test_startup_check_requires_token(settings):
    from django.core.checks import run_checks

    settings.CREDIT_PROVIDER = "surepass"
    settings.SUREPASS_API_TOKEN = ""
    assert "core.E010" in [e.id for e in run_checks(tags=["security"])]

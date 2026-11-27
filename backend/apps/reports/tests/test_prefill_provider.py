"""Two separate providers: Surepass finds the details by mobile number (PREFILL_PROVIDER), SmartAuth pulls
the report with them (CREDIT_PROVIDER). The response below has the exact shape of the Surepass
prefill-by-mobile sample supplied on 2026-11-26, with made-up values."""

import pytest
from django.core import checks

from apps.messaging.dev import simulate_inbound
from apps.reports.models import ReportRequest, ReportStatus
from apps.reports.providers import get_prefill_provider, smartauth, surepass
from apps.reports.tests.test_smartauth import FakeResponse, ok_body

SUREPASS_PREFILL = {
    "data": {
        "client_id": "prefill_report_v2_TESTCLIENT",
        "name": "ANITA DESAI",
        "mobile": "9800066601",
        "personal_info": {
            "full_name": "ANITA DESAI ",  # trailing space, as in the real sample
            "dob": "1980-01-01",
            "gender": "Female",
            "total_income": "",
            "occupation": "",
            "age": "45",
        },
        "phone_info": [{"reported_date": "2024-01-28", "type_code": "M", "number": "9800066601"}],
        "address_info": [
            {"address": "1 TEST STREET", "state": "TEST", "type": "Permanent", "postal": "100001"}
        ],
        "email_info": [{"reported_date": "2021-06-30", "email_address": "ANITA@EXAMPLE.COM"}],
        "identity_info": {
            "pan_number": [{"id_number": "ABCPD1234E"}],
            "passport_number": [],
            "driving_license": [],
            "voter_id": [{"id_number": "TEST0000000001"}],
            "aadhaar_number": [{"id_number": "XXXXXXXXXXXX"}],
            "ration_card": [],
            "other_id": [],
        },
    },
    "status_code": 200,
    "success": True,
    "message": "Success",
    "message_code": "success",
}


class RoutingSession:
    """One fake for both providers: answers by URL, records every call."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw.get("json"), kw.get("headers", {})))
        if "prefill-by-mobile" in url:
            return FakeResponse(200, SUREPASS_PREFILL)
        if url.endswith("/TILJ"):
            return FakeResponse(200, ok_body())
        raise AssertionError(f"unexpected call {url}")

    def get(self, url, **kw):
        raise AssertionError(f"unexpected download {url}")


@pytest.fixture
def two_providers(settings, monkeypatch):
    settings.CREDIT_PROVIDER = "smartauth"
    settings.PREFILL_PROVIDER = "surepass"
    settings.PREFILL_BY_MOBILE = True
    settings.BUREAU_B_BASE_URL = "https://api.smartauth.example/v1"
    settings.BUREAU_B_AUTHKEY = "sa-key"
    settings.SUREPASS_API_TOKEN = "aaa.bbb.ccc"
    settings.SUREPASS_PREFILL_TOKEN = ""
    session = RoutingSession()
    monkeypatch.setattr(smartauth.requests, "Session", lambda: session)
    monkeypatch.setattr(surepass.requests, "Session", lambda: session)
    return session


def test_surepass_sample_shape_is_read_and_everything_else_discarded(two_providers):
    found = get_prefill_provider().prefill_by_mobile("919800066601", "consent-1")
    assert (found.full_name, found.pan, found.gender) == ("ANITA DESAI", "ABCPD1234E", "F")
    assert repr(found) == "Prefill(<redacted>)"  # address, e-mail, voter ID, DOB are never kept
    url, body, headers = two_providers.calls[0]
    assert url == "https://kyc-api.surepass.app/api/v1/prefill/prefill-by-mobile"
    assert body == {"mobile": "9800066601"} and headers["Authorization"] == "Bearer aaa.bbb.ccc"


@pytest.mark.django_db
def test_surepass_details_then_smartauth_report(two_providers):
    wa = "919800066601"

    def say(text=None, button=None):
        return " || ".join(r.get("body", "") for r in simulate_inbound(wa, text=text, reply_id=button))

    assert "look up the details" in say("Hi")  # consent first: nothing is looked up before it
    assert two_providers.calls == []
    shown = say(button="agree")
    assert "Is this you?" in shown and "ANITA DESAI" in shown and "ABCPD1**4E" in shown
    assert [c[0].rsplit("/", 1)[-1] for c in two_providers.calls] == ["prefill-by-mobile"]  # Surepass only

    assert "fetching your credit report" in say(button="confirm")
    url, body, headers = two_providers.calls[-1]
    assert url.endswith("/TILJ") and headers["authkey"] == "sa-key"  # then SmartAuth, separately
    assert (body["name"], body["pan"], body["mobile"]) == ("ANITA DESAI", "ABCPD1234E", "9800066601")
    req = ReportRequest.objects.get()
    assert req.report_status == ReportStatus.GENERATED and req.provider_name == "smartauth"


def test_without_a_prefill_provider_smartauth_asks_for_details(settings):
    settings.CREDIT_PROVIDER = "smartauth"
    settings.PREFILL_PROVIDER = ""
    assert get_prefill_provider() is None


def test_report_provider_with_its_own_lookup_still_works_alone(settings):
    settings.CREDIT_PROVIDER = "mock_no_otp"
    settings.PREFILL_PROVIDER = ""
    assert get_prefill_provider().name == "mock_no_otp"


@pytest.mark.parametrize(
    ("token", "warned"),
    [("aaa.bbb.ccc", False), ("aaa.bbb>", True), ("aaa.bbb", True)],
)
def test_cut_off_surepass_token_is_reported_at_startup(settings, token, warned):
    settings.CREDIT_PROVIDER = "smartauth"
    settings.PREFILL_PROVIDER = "surepass"
    settings.SUREPASS_API_TOKEN = token
    settings.SUREPASS_PREFILL_TOKEN = ""
    from apps.core.checks import check_surepass

    ids = [m.id for m in check_surepass(None)]
    assert ("core.W014" in ids) is warned
    assert all(m.level < checks.ERROR for m in check_surepass(None))  # a warning, not a blocked start


def test_live_surepass_shape_with_details_wrapper_is_read(two_providers, monkeypatch):
    """The live API (2026-11-27) nests the person under data.details, unlike the earlier sample. Missing
    that made every real lookup look like "not found". Same structure as the live response, made-up values."""
    live = {
        "data": {
            "client_id": "prefill_by_mobile_TESTCLIENT",
            "mobile": "9800066602",
            "details": {
                "personal_info": {"full_name": "VIKRAM MEHTA ", "dob": "1990-01-01", "gender": "Male"},
                "phone_info": [{"reported_date": "2026-03-15", "type_code": "M", "number": "9800066602"}],
                "address_info": [
                    {"address": "1 TEST ROAD", "state": "XX", "type": "Primary", "postal": "100001"}
                ],
                "email_info": [{"reported_date": "2026-06-30", "email_address": "VIKRAM@EXAMPLE.COM"}],
                "identity_info": {
                    "pan_number": [{"id_number": "ABCPM1234Z"}],
                    "aadhaar_number": [{"id_number": "XXXXXXXXXXXX"}],
                    "other_id": [{"id_number": "00000000000000"}],
                },
            },
        },
        "status_code": 200,
        "success": True,
        "message": "Success",
        "message_code": "success",
    }
    monkeypatch.setattr(two_providers, "post", lambda url, **kw: FakeResponse(200, live))
    found = get_prefill_provider().prefill_by_mobile("919800066602", "consent-2")
    assert (found.full_name, found.pan, found.gender) == ("VIKRAM MEHTA", "ABCPM1234Z", "M")

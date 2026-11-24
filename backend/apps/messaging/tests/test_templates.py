"""Template registry, parameter validation before sending, and the Meta submission script (offline)."""

import importlib.util
import io
import json
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from apps.conversations.models import Conversation
from apps.core.testing.factories import CustomerFactory
from apps.messaging.gateway.mock import outbox
from apps.messaging.models import MessageLog
from apps.messaging.services import MessagingService, TemplateParamMismatch
from apps.messaging.templates import TEMPLATES, TemplateParamError, check_definition, validate_params

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "submit_whatsapp_templates.py"
TOKEN = "EAAG-test-token-must-never-be-printed"  # noqa: S105


def load_script():
    spec = importlib.util.spec_from_file_location("submit_whatsapp_templates", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- registry -------------------------------------------------------------------------------------------


def test_registry_matches_settings_and_the_four_templates():
    assert set(TEMPLATES) == {"payment_link", "payment_reminder", "report_delivery", "report_expired"}
    assert {k: t.default_name for k, t in TEMPLATES.items()} == settings.WHATSAPP_TEMPLATES
    assert {k: t.param_count for k, t in TEMPLATES.items()} == {
        "payment_link": 3,
        "payment_reminder": 3,
        "report_delivery": 1,
        "report_expired": 1,
    }
    assert [k for k, t in TEMPLATES.items() if t.has_document_header] == ["report_delivery"]
    for t in TEMPLATES.values():
        check_definition(t)
        assert t.category == "UTILITY" and t.language == settings.WHATSAPP_TEMPLATE_LANGUAGE


def test_env_overrides_the_template_name():
    from apps.messaging.templates import default_names

    env = {"WA_TEMPLATE_REPORT_EXPIRED": "report_expired_v2"}
    names = default_names(lambda var, default: env.get(var, default))
    assert names["report_expired"] == "report_expired_v2" and names["payment_link"] == "payment_link_v1"


@pytest.mark.parametrize(
    "body",
    ["{{1}} is ready", "Your report is {{1}}", "Report {{2}} ready {{1}}.", "Report {{1}} and {{3}}."],
)
def test_definitions_meta_would_reject_are_caught(body):
    from dataclasses import replace

    t = replace(TEMPLATES["report_expired"], body=body, body_examples=("a",) * body.count("{{"))
    with pytest.raises(TemplateParamError):
        check_definition(t)


@pytest.mark.parametrize(
    "params",
    [
        [],
        ["CR-1", "₹99"],
        ["CR-1", "₹99", "https://x", "extra"],
        ["CR-1", "", "https://x"],
        ["CR-1", "   ", "https://x"],
        ["CR-1", "₹99\n", "https://x"],
        ["CR-1", "₹\t99", "https://x"],
        ["CR-1", "₹     99", "https://x"],
    ],
)
def test_validate_params_rejects_bad_values(params):
    with pytest.raises(TemplateParamError):
        validate_params("payment_link", params)


def test_validate_params_accepts_and_stringifies():
    assert validate_params("report_expired", ["CR-1"]) == ["CR-1"]
    assert validate_params("payment_link", ["CR-1", 99, "https://x"]) == ["CR-1", "99", "https://x"]
    with pytest.raises(TemplateParamError):
        validate_params("no_such_template", [])


# --- validation in MessagingService ---------------------------------------------------------------------


def _customer(hours_ago):
    c = CustomerFactory()
    Conversation.objects.create(customer=c, last_inbound_at=timezone.now() - timedelta(hours=hours_ago))
    return c


@pytest.mark.django_db
@pytest.mark.parametrize("hours_ago", [1, 30])  # inside and outside the window
def test_send_with_wrong_param_count_is_refused_before_logging_or_sending(hours_ago):
    c = _customer(hours_ago)
    before = len(outbox())
    with pytest.raises(TemplateParamMismatch) as exc:
        MessagingService().send(c, text="Hi", template_key="payment_link", template_params=["CR-1"])
    assert exc.value.code == "template_param_mismatch"
    assert not MessageLog.objects.exists() and len(outbox()) == before


@pytest.mark.django_db
def test_send_document_with_wrong_params_is_refused_before_upload():
    c = _customer(30)
    before = len(outbox())
    with pytest.raises(TemplateParamMismatch):
        MessagingService().send_document(
            c,
            content=b"%PDF-1.4",
            filename="r.pdf",
            caption="x",
            template_params=[],
            idempotency_key="deliver:x",
        )
    assert not MessageLog.objects.exists() and len(outbox()) == before


@pytest.mark.django_db
def test_expired_template_outside_window_sends_the_reference():
    c = _customer(30)
    MessagingService().send(
        c, text="expired", template_key="report_expired", template_params=["CR-9"], idempotency_key="k"
    )
    sent = outbox()[-1]
    assert sent["kind"] == "template" and sent["template"] == "report_expired_v1"
    assert sent["params"] == ["CR-9"]


# --- submission script (no network) ---------------------------------------------------------------------


def test_script_payloads_have_examples_for_every_variable():
    script = load_script()
    for t in TEMPLATES.values():
        payload = script.build_payload(t, header_handle="4::handle")
        body = next(c for c in payload["components"] if c["type"] == "BODY")
        assert body["text"] == t.body and len(body["example"]["body_text"][0]) == t.param_count
        assert payload["name"] == t.default_name and payload["category"] == "UTILITY"
        header = [c for c in payload["components"] if c["type"] == "HEADER"]
        if t.has_document_header:
            assert header == [
                {"type": "HEADER", "format": "DOCUMENT", "example": {"header_handle": ["4::handle"]}}
            ]
        else:
            assert header == []


def test_specimen_pdf_is_a_valid_pdf():
    data = load_script().specimen_pdf()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    xref_at = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert data[xref_at:].startswith(b"xref")
    assert b"SPECIMEN" in data


def test_dry_run_makes_no_calls_and_never_prints_the_token(monkeypatch):
    script = load_script()
    monkeypatch.setenv("META_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("WABA_ID", "123")
    monkeypatch.setattr(script, "Graph", lambda *a, **k: pytest.fail("dry run must not call Meta"))
    out = io.StringIO()
    with redirect_stdout(out):
        assert script.main([]) == 0
    text = out.getvalue()
    assert TOKEN not in text and "DRY RUN" in text
    for t in TEMPLATES.values():
        assert t.default_name in text


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body, self.reason = status, body, "x"

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.headers, self.calls = {}, []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/uploads"):
            return FakeResponse(200, {"id": "upload:abc"})
        if url.endswith("/upload:abc"):
            return FakeResponse(200, {"h": "4::handle"})
        name = kw["json"]["name"]
        if name == "payment_reminder_v1":
            return FakeResponse(400, {"error": {"message": "Template already exists", "code": 100}})
        return FakeResponse(200, {"id": f"id-{name}", "status": "PENDING", "category": "UTILITY"})


def test_submit_is_sequential_reports_each_result_and_hides_the_token(monkeypatch):
    import requests

    script = load_script()
    session = FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: session)
    for k, v in {"WABA_ID": "123", "META_ACCESS_TOKEN": TOKEN, "GRAPH_API_VERSION": "v21.0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("META_APP_ID", "456")
    out = io.StringIO()
    with redirect_stdout(out):
        assert script.main(["--submit"]) == 1  # one failure -> non-zero exit, others still submitted
    text = out.getvalue()
    assert TOKEN not in text
    assert "payment_link_v1: id=id-payment_link_v1 status=PENDING category=UTILITY" in text
    assert "payment_reminder_v1: FAILED HTTP 400: Template already exists" in text
    assert "report_expired_v1: id=id-report_expired_v1 status=PENDING" in text
    assert session.headers["Authorization"] == f"Bearer {TOKEN}"
    for _, url, kw in session.calls:  # token only in the header, never in a URL or query string
        assert TOKEN not in url and TOKEN not in json.dumps(kw.get("params", {}))
    created = [kw["json"] for _, url, kw in session.calls if url.endswith("/123/message_templates")]
    assert [p["name"] for p in created] == [t.default_name for t in TEMPLATES.values()]
    delivery = next(p for p in created if p["name"] == "report_delivery_v1")
    assert delivery["components"][0]["example"]["header_handle"] == ["4::handle"]
    upload = next(kw for _, url, kw in session.calls if url.endswith("/upload:abc"))
    assert upload["headers"]["file_offset"] == "0" and upload["data"].startswith(b"%PDF")


def test_submit_without_env_exits_without_calling(monkeypatch):
    script = load_script()
    for n in ("WABA_ID", "META_ACCESS_TOKEN", "GRAPH_API_VERSION", "META_APP_ID"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(SystemExit) as exc:
        script.main(["--submit"])
    assert "Missing environment variable" in str(exc.value)

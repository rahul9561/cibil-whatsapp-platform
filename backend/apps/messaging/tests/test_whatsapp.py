"""WhatsApp webhook (handshake, signatures, dedupe, statuses) and the Cloud API adapter."""

import hashlib
import hmac
import json
from unittest import mock

import pytest
import requests
from django.test import Client

from apps.audit.models import AuditLog
from apps.core.integrations import PermanentError, TransientError
from apps.core.models import WebhookEvent, WebhookProcessingStatus
from apps.core.testing.api import make_request
from apps.customers.models import Customer
from apps.messaging.gateway.base import Button, OutsideServiceWindow
from apps.messaging.gateway.cloud_api import CloudApiWhatsAppGateway
from apps.messaging.gateway.mock import outbox
from apps.messaging.models import MessageLog, MessageStatus
from apps.payments.gateway.mock import simulate_payment
from apps.payments.tasks import confirm_payment_task
from apps.reports.models import DeliveryStatus

URL = "/webhooks/whatsapp"
SECRET, VERIFY = "app-secret-test", "verify-token-test"


@pytest.fixture(autouse=True)
def wa_settings(settings):
    settings.WHATSAPP_APP_SECRET = SECRET
    settings.WHATSAPP_VERIFY_TOKEN = VERIFY
    settings.WHATSAPP_PHONE_NUMBER_ID = "1111"


def envelope(messages=(), statuses=()):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1111"},
                            "contacts": [{"profile": {"name": "Rahul"}, "wa_id": "919812300009"}],
                            "messages": list(messages),
                            "statuses": list(statuses),
                        },
                    }
                ],
            }
        ],
    }


def text_msg(body, wamid="wamid.A", frm="919812300009"):
    return {"from": frm, "id": wamid, "timestamp": "1900000000", "type": "text", "text": {"body": body}}


def post(payload, secret=SECRET, client=None):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return (client or Client(enforce_csrf_checks=True)).post(
        URL, raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig
    )


# --- webhook ---------------------------------------------------------------------------------------------


def test_subscription_handshake():
    c = Client()
    ok = c.get(URL, {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.content == b"12345"
    assert (
        c.get(URL, {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}).status_code
        == 403
    )


@pytest.mark.django_db
def test_unsigned_or_forged_posts_are_rejected():
    assert post(envelope([text_msg("hi")]), secret="forged").status_code == 400
    assert Client().post(URL, b"{}", content_type="application/json").status_code == 400
    assert not WebhookEvent.objects.exists()
    assert AuditLog.objects.filter(action="webhook.signature_invalid").count() == 2


@pytest.mark.django_db
def test_empty_app_secret_rejects_everything(settings):
    settings.WHATSAPP_APP_SECRET = ""
    assert post(envelope([text_msg("hi")]), secret="").status_code == 400


@pytest.mark.django_db
def test_message_is_processed_once_and_text_is_not_stored(django_capture_on_commit_callbacks):
    # A distinctive text: searching for "hi" in the payload used to fail at random, because the encrypted
    # sender (random ciphertext) sometimes contains those two letters.
    text = "Hi there 7Q4ZK9"
    with django_capture_on_commit_callbacks(execute=True):
        assert post(envelope([text_msg(text)])).json() == {"status": "accepted", "events": 1}
    with django_capture_on_commit_callbacks(execute=True):
        assert post(envelope([text_msg(text)])).json() == {"status": "accepted", "events": 0}  # Meta retry
    event = WebhookEvent.objects.get()
    assert event.processing_status == WebhookProcessingStatus.PROCESSED
    assert "7Q4ZK9" not in json.dumps(event.payload)
    assert set(event.payload) == {"wa_message_id", "from_enc", "type", "reply_id", "timestamp"}
    assert "919812300009" not in json.dumps(event.payload)  # sender stored encrypted
    replies = [m for m in outbox() if m["to"] == "919812300009"]
    assert len(replies) == 2 and "full name" in replies[-1]["body"]


@pytest.mark.django_db
def test_retried_task_resends_missing_replies_without_rerunning_bot(django_capture_on_commit_callbacks):
    from apps.conversations.bot import process_inbound_event

    with django_capture_on_commit_callbacks(execute=True):
        post(envelope([text_msg("hi")]))
    sent_before = len(outbox())
    assert process_inbound_event(WebhookEvent.objects.get()) == "resent"
    assert len(outbox()) == sent_before  # every reply already sent: nothing duplicated
    assert Customer.objects.count() == 1


@pytest.mark.django_db
def test_statuses_advance_and_delivery_releases_the_request(django_capture_on_commit_callbacks):
    req = make_request(wa_id="919812300010")
    link = req.payment_links.get()
    simulate_payment(link.razorpay_link_id)
    confirm_payment_task.delay(link.razorpay_link_id)
    doc_msg = MessageLog.objects.get(idempotency_key=f"deliver:{req.pk}")
    req.refresh_from_db()
    assert req.delivery_status == DeliveryStatus.SENT and req.active_lock is None  # sent = done

    def status(name, n):
        return envelope(
            statuses=[
                {
                    "id": doc_msg.wa_message_id,
                    "status": name,
                    "timestamp": str(1900000000 + n),
                    "recipient_id": "919812300010",
                }
            ]
        )

    with django_capture_on_commit_callbacks(execute=True):
        post(status("read", 2))
        post(status("delivered", 1))  # arrives late: must not move READ back to DELIVERED
    doc_msg.refresh_from_db()
    req.refresh_from_db()
    assert doc_msg.status == MessageStatus.READ and doc_msg.delivered_at and doc_msg.read_at
    assert req.delivery_status == DeliveryStatus.READ and req.delivered_at


@pytest.mark.django_db
def test_failed_status_is_recorded(django_capture_on_commit_callbacks):
    req = make_request(wa_id="919812300011")
    msg = req.messages.exclude(wa_message_id=None).first()
    with django_capture_on_commit_callbacks(execute=True):
        post(
            envelope(
                statuses=[
                    {
                        "id": msg.wa_message_id,
                        "status": "failed",
                        "timestamp": "1900000000",
                        "errors": [{"code": 131026, "title": "Undeliverable"}],
                    }
                ]
            )
        )
    msg.refresh_from_db()
    assert msg.status == MessageStatus.FAILED and msg.error_code == "131026"


@pytest.mark.django_db
def test_other_phone_number_ids_are_ignored(django_capture_on_commit_callbacks):
    payload = envelope([text_msg("hi")])
    payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "9999"
    assert post(payload).json()["events"] == 0


# --- Cloud API adapter -----------------------------------------------------------------------------------


def fake_session(status=200, body=None, exc=None):
    session = mock.Mock()
    if exc:
        session.post.side_effect = exc
    else:
        resp = mock.Mock(status_code=status)
        resp.json.return_value = body if body is not None else {"messages": [{"id": "wamid.X"}]}
        session.post.return_value = resp
    return session


@pytest.fixture
def cloud(settings):
    settings.WHATSAPP_ACCESS_TOKEN = "token"
    settings.WHATSAPP_GRAPH_VERSION = "v21.0"


def test_payload_shapes(cloud):
    s = fake_session()
    gw = CloudApiWhatsAppGateway(session=s)
    assert gw.send_text("919812300009", "Hello").wa_message_id == "wamid.X"
    url, kwargs = s.post.call_args[0][0], s.post.call_args[1]
    assert url == "https://graph.facebook.com/v21.0/1111/messages"
    assert kwargs["headers"]["Authorization"] == "Bearer token" and kwargs["timeout"] == (5, 30)
    assert kwargs["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "919812300009",
        "type": "text",
        "text": {"body": "Hello", "preview_url": False},
    }

    gw.send_buttons(
        "9198", "Pick", [Button("agree", "I agree"), Button("decline", "A very long title indeed")]
    )
    buttons = s.post.call_args[1]["json"]["interactive"]["action"]["buttons"]
    assert buttons[1]["reply"] == {"id": "decline", "title": "A very long title in"}  # 20-char limit

    gw.send_template(
        "9198", "report_delivery_v1", "en", ["CR-1"], document_media_id="m1", document_filename="r.pdf"
    )
    tpl = s.post.call_args[1]["json"]["template"]
    assert tpl["name"] == "report_delivery_v1" and tpl["language"] == {"code": "en"}
    assert tpl["components"][0]["parameters"][0]["document"] == {"id": "m1", "filename": "r.pdf"}
    assert tpl["components"][1]["parameters"] == [{"type": "text", "text": "CR-1"}]


def test_media_upload(cloud):
    s = fake_session(body={"id": "media-1"})
    assert CloudApiWhatsAppGateway(session=s).upload_media(b"%PDF", "application/pdf", "r.pdf") == "media-1"
    assert s.post.call_args[0][0].endswith("/1111/media")
    assert s.post.call_args[1]["files"]["file"] == ("r.pdf", b"%PDF", "application/pdf")
    with pytest.raises(TransientError):
        CloudApiWhatsAppGateway(session=fake_session(body={})).upload_media(b"x", "application/pdf", "r.pdf")


@pytest.mark.parametrize(
    ("kwargs", "expected", "code"),
    [
        (
            {"status": 400, "body": {"error": {"code": 131047}}},
            OutsideServiceWindow,
            "outside_service_window",
        ),
        ({"status": 400, "body": {"error": {"code": 130429}}}, TransientError, "whatsapp_130429"),
        ({"status": 503, "body": {}}, TransientError, "whatsapp_503"),
        ({"status": 401, "body": {"error": {"code": 190}}}, PermanentError, "whatsapp_auth"),
        ({"status": 400, "body": {"error": {"code": 131026}}}, PermanentError, "whatsapp_131026"),
        ({"exc": requests.exceptions.ReadTimeout()}, TransientError, "whatsapp_unreachable"),
        ({"status": 200, "body": {"messages": []}}, TransientError, "whatsapp_bad_response"),
    ],
)
def test_error_mapping(cloud, kwargs, expected, code):
    with pytest.raises(expected) as exc:
        CloudApiWhatsAppGateway(session=fake_session(**kwargs)).send_text("9198", "x")
    assert exc.value.code == code


def test_startup_check_requires_credentials(settings):
    from django.core.checks import run_checks

    settings.WHATSAPP_GATEWAY = "cloud_api"
    settings.WHATSAPP_ACCESS_TOKEN = settings.WHATSAPP_APP_SECRET = ""
    assert [e.id for e in run_checks(tags=["security"])].count("core.E009") == 2

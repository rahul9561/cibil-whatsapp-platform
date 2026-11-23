"""RazorpayGateway against a fake SDK client: request shape, mapping, error translation, signatures."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest
import razorpay.errors as rz
import requests

from apps.core.integrations import PermanentError, TransientError
from apps.payments.gateway.razorpay_gateway import TIMEOUT, RazorpayGateway, verify_signature


class Namespace:
    def __init__(self, **methods):
        self.calls = []
        for name, impl in methods.items():
            setattr(self, name, self._recording(name, impl))

    def _recording(self, name, impl):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if isinstance(impl, Exception):
                raise impl
            return impl(*args) if callable(impl) else impl

        return method


class FakeClient:
    def __init__(self, payment_link=None, payment=None):
        self.payment_link = payment_link or Namespace()
        self.payment = payment or Namespace()


LINK = {
    "id": "plink_1",
    "short_url": "https://rzp.io/i/abc",
    "status": "created",
    "reference_id": "CR-ABC-1",
    "amount": 9900,
    "amount_paid": 0,
    "currency": "INR",
    "expire_by": 1900000000,
    "payments": None,
}
EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)


def create_kwargs(**over):
    return {
        "reference_id": "CR-ABC-1",
        "amount_paise": 9900,
        "currency": "INR",
        "description": "Credit report",
        "customer_phone": "919876543210",
        "expires_at": EXPIRES,
        **over,
    }


def test_create_link_request_shape_and_timeout():
    links = Namespace(create=LINK)
    gw = RazorpayGateway(client=FakeClient(payment_link=links))
    created = gw.create_payment_link(**create_kwargs())
    assert created.link_id == "plink_1" and created.short_url == "https://rzp.io/i/abc"
    name, args, kwargs = links.calls[0]
    data = args[0]
    assert kwargs["timeout"] == TIMEOUT
    assert data["amount"] == 9900 and data["currency"] == "INR" and data["accept_partial"] is False
    assert data["reference_id"] == "CR-ABC-1" and data["customer"] == {"contact": "+919876543210"}
    assert data["notify"] == {"sms": False, "email": False} and data["reminder_enable"] is False
    assert data["expire_by"] == int(EXPIRES.timestamp())


def test_duplicate_reference_retry_returns_existing_link():
    links = Namespace(
        create=rz.BadRequestError("reference_id already exists"),
        all={"payment_links": [{**LINK, "reference_id": "other"}, LINK]},
    )
    created = RazorpayGateway(client=FakeClient(payment_link=links)).create_payment_link(**create_kwargs())
    assert created.link_id == "plink_1"
    assert [c[0] for c in links.calls] == ["create", "all"]


def test_other_bad_request_is_permanent():
    links = Namespace(create=rz.BadRequestError("amount must be at least 100"))
    with pytest.raises(PermanentError) as exc:
        RazorpayGateway(client=FakeClient(payment_link=links)).create_payment_link(**create_kwargs())
    assert exc.value.code == "razorpay_bad_request"


@pytest.mark.parametrize(
    ("error", "kind", "code"),
    [
        (requests.exceptions.ReadTimeout(), TransientError, "razorpay_unreachable"),
        (requests.exceptions.ConnectionError(), TransientError, "razorpay_unreachable"),
        (rz.ServerError("boom"), TransientError, "razorpay_server_error"),
        (rz.GatewayError("upstream"), TransientError, "razorpay_server_error"),
        (ValueError("not json"), TransientError, "razorpay_bad_response"),
        (rz.BadRequestError("invalid id"), PermanentError, "razorpay_bad_request"),
    ],
)
def test_error_translation(error, kind, code):
    gw = RazorpayGateway(client=FakeClient(payment_link=Namespace(fetch=error)))
    with pytest.raises(kind) as exc:
        gw.fetch_payment_link("plink_1")
    assert exc.value.code == code


def test_fetch_uses_authoritative_payment_records():
    paid_link = {
        **LINK,
        "status": "paid",
        "amount_paid": 9900,
        "payments": [{"payment_id": "pay_1", "amount": 1, "status": "captured", "method": "upi"}],
    }
    payments = Namespace(
        fetch={
            "id": "pay_1",
            "amount": 9900,
            "currency": "INR",
            "status": "captured",
            "method": "upi",
            "order_id": "order_1",
        }
    )
    state = RazorpayGateway(
        client=FakeClient(payment_link=Namespace(fetch=paid_link), payment=payments)
    ).fetch_payment_link("plink_1")
    assert state.status == "paid" and state.reference_id == "CR-ABC-1" and state.amount_paid_paise == 9900
    p = state.payments[0]
    assert (p.payment_id, p.amount_paise, p.currency, p.order_id) == ("pay_1", 9900, "INR", "order_1")
    assert payments.calls[0][2]["timeout"] == TIMEOUT


def test_cancel_ignores_already_final_links():
    links = Namespace(cancel=rz.BadRequestError("Payment link cannot be cancelled"))
    RazorpayGateway(client=FakeClient(payment_link=links)).cancel_payment_link("plink_1")  # no raise


def test_refund_and_find_refund():
    payments = Namespace(
        refund={"id": "rfnd_1", "status": "pending"},
        fetch_multiple_refund={
            "items": [
                {"id": "rfnd_0", "notes": {"refund_ref": "other"}},
                {"id": "rfnd_1", "status": "processed", "notes": {"refund_ref": "R1"}},
            ]
        },
    )
    gw = RazorpayGateway(client=FakeClient(payment=payments))
    result = gw.create_refund("pay_1", 500, reference="R1")
    assert (result.refund_id, result.status) == ("rfnd_1", "pending")
    _, args, kwargs = payments.calls[0]
    assert args == ("pay_1", {"amount": 500, "speed": "normal", "notes": {"refund_ref": "R1"}})
    assert kwargs["timeout"] == TIMEOUT
    assert gw.find_refund("pay_1", "R1").status == "processed"
    assert gw.find_refund("pay_1", "R9") is None


def test_signature_verification():
    body = b'{"event":"payment_link.paid"}'
    good = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, good, "whsec")
    assert not verify_signature(body + b" ", good, "whsec")
    assert not verify_signature(body, good, "other-secret")
    assert not verify_signature(body, "", "whsec")
    assert not verify_signature(body, good, "")  # unconfigured secret never verifies


def test_startup_checks_require_secrets(settings):
    from django.core.checks import run_checks

    settings.PAYMENT_GATEWAY = "razorpay"
    settings.RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_SECRET = settings.RAZORPAY_WEBHOOK_SECRET = ""
    ids = [e.id for e in run_checks(tags=["security"])]
    assert ids.count("core.E007") == 3
    settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET, settings.RAZORPAY_WEBHOOK_SECRET = (
        "rzp_live_x",
        "s",
        "w",
    )
    settings.DEBUG = True
    assert "core.E008" in [e.id for e in run_checks(tags=["security"])]


def test_expiry_timestamp_round_trip():
    links = Namespace(create={**LINK, "expire_by": int((EXPIRES + timedelta(hours=1)).timestamp())})
    created = RazorpayGateway(client=FakeClient(payment_link=links)).create_payment_link(**create_kwargs())
    assert created.expires_at == EXPIRES + timedelta(hours=1)

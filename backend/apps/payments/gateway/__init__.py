from apps.core.integrations import load_adapter

from .base import PaymentGateway

GATEWAYS = {
    "mock": "apps.payments.gateway.mock.MockPaymentGateway",
    "razorpay": "apps.payments.gateway.razorpay_gateway.RazorpayGateway",
}


def get_payment_gateway() -> PaymentGateway:
    return load_adapter("PAYMENT_GATEWAY", GATEWAYS)

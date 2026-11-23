from apps.core.integrations import load_adapter

from .base import WhatsAppGateway

GATEWAYS = {
    "mock": "apps.messaging.gateway.mock.MockWhatsAppGateway",
    # "cloud_api": added in Phase 7.
}


def get_whatsapp() -> WhatsAppGateway:
    return load_adapter("WHATSAPP_GATEWAY", GATEWAYS)

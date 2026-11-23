from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# Local development runs entirely on mock adapters until real credentials exist.
CREDIT_PROVIDER = env("CREDIT_PROVIDER", default="mock")
PAYMENT_GATEWAY = env("PAYMENT_GATEWAY", default="mock")
WHATSAPP_GATEWAY = env("WHATSAPP_GATEWAY", default="mock")
REPORT_PRICE_PAISE = env.int("REPORT_PRICE_PAISE", default=9900)  # placeholder until pricing is decided

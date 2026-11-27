"""Test settings.

Defaults to SQLite for fast local runs; set DATABASE_URL to run against MySQL (CI/docker).
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-production")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
# Fixed, test-only keys (never used outside the test suite).
os.environ.setdefault(
    "FIELD_ENCRYPTION_KEYS",
    "dGVzdC1rZXktMS0wMDAwMDAwMDAwMDAwMDAwMDAwMDA=,dGVzdC1rZXktMi0wMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
)
os.environ.setdefault("BLIND_INDEX_KEY", "test-blind-index-key-0123456789abcdef")
os.environ.setdefault(
    "REPORT_FILE_ENCRYPTION_KEYS",
    "t1:dGVzdC1maWxlLWtleS0xLTAwMDAwMDAwMDAwMDAwMDA=,t0:dGVzdC1maWxlLWtleS0wLTAwMDAwMDAwMDAwMDAwMDA=",
)

from .base import *  # noqa: E402, F403
from .base import BASE_DIR  # noqa: E402

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
PRIVATE_STORAGE_ROOT = BASE_DIR / ".pytest_cache" / "private_reports"
LOG_FORMAT = "console"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
INSTALLED_APPS = [*INSTALLED_APPS, "apps.core.tests.testapp"]  # noqa: F405
CREDIT_PROVIDER = "mock"
PAYMENT_GATEWAY = "mock"
WHATSAPP_GATEWAY = "mock"
REPORT_PRICE_PAISE = 9900
ALLOW_MOCK_ADAPTERS = True

# Tests always use their own fixed keys, even when real ones are in the environment (e.g. in the container).
FIELD_ENCRYPTION_KEYS = [
    "dGVzdC1rZXktMS0wMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "dGVzdC1rZXktMi0wMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
]
BLIND_INDEX_KEY = "test-blind-index-key-0123456789abcdef"
REPORT_FILE_ENCRYPTION_KEYS = [
    "t1:dGVzdC1maWxlLWtleS0xLTAwMDAwMDAwMDAwMDAwMDA=",
    "t0:dGVzdC1maWxlLWtleS0wLTAwMDAwMDAwMDAwMDAwMDA=",
]
# Most flow tests start in English; language tests switch this on.
WHATSAPP_ASK_LANGUAGE = False

# Tests never talk to a real service, whatever the container's .env holds (2026-11-26: PREFILL_PROVIDER and a
# working SUREPASS token leaked in from the environment and the suite made real Surepass lookups).
# conftest.py additionally blocks every real HTTP request.
PREFILL_PROVIDER = ""
SUREPASS_API_TOKEN = ""
SUREPASS_PREFILL_TOKEN = ""
BUREAU_B_BASE_URL = ""
BUREAU_B_AUTHKEY = ""
RAZORPAY_KEY_ID = ""
RAZORPAY_KEY_SECRET = ""
RAZORPAY_WEBHOOK_SECRET = ""
WHATSAPP_ACCESS_TOKEN = ""
WHATSAPP_APP_SECRET = ""
WHATSAPP_VERIFY_TOKEN = ""
WHATSAPP_PHONE_NUMBER_ID = ""

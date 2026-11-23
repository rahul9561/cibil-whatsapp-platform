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

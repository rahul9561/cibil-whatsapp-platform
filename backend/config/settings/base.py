"""Settings shared by every environment. Environment-specific modules import from here."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/
REPO_ROOT = BASE_DIR.parent

env = environ.Env()
# A .env at the repo root is read for local (non-docker) runs; docker compose injects env vars directly.
environ.Env.read_env(REPO_ROOT / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "apps.core",
    "apps.accounts",
    "apps.audit",
    "apps.customers",
    "apps.reports",
    "apps.payments",
    "apps.messaging",
    "apps.conversations",
    "apps.followups",
]

MIDDLEWARE = [
    "apps.core.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- Database ---
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"].setdefault("OPTIONS", {})
if DATABASES["default"]["ENGINE"] == "django.db.backends.mysql":
    DATABASES["default"]["OPTIONS"].update(
        {"charset": "utf8mb4", "init_command": "SET sql_mode='STRICT_TRANS_TABLES'"}
    )
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth ---
AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

OTP_TOTP_ISSUER = env("OTP_TOTP_ISSUER", default="CIBIL Admin")
LOGIN_LOCKOUT_THRESHOLD = env.int("LOGIN_LOCKOUT_THRESHOLD", default=5)
LOGIN_LOCKOUT_SECONDS = env.int("LOGIN_LOCKOUT_SECONDS", default=15 * 60)
# Number of reverse proxies in front of Django whose X-Forwarded-For entry is trusted (nginx = 1).
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)

# --- Encryption (ARCHITECTURE §6.4). Keys only ever come from the environment. ---
FIELD_ENCRYPTION_KEYS = env.list("FIELD_ENCRYPTION_KEYS")  # newest first
BLIND_INDEX_KEY = env("BLIND_INDEX_KEY")

# --- Report files (ARCHITECTURE §6.3): AES-256-GCM keys as "key_id:base64-32-bytes", current key first.
REPORT_FILE_ENCRYPTION_KEYS = env.list("REPORT_FILE_ENCRYPTION_KEYS")

# --- Adapters: "mock" or a real adapter name (added in Phases 6, 7, 10). Empty = not configured. ---
CREDIT_PROVIDER = env("CREDIT_PROVIDER", default="")
PAYMENT_GATEWAY = env("PAYMENT_GATEWAY", default="")
WHATSAPP_GATEWAY = env("WHATSAPP_GATEWAY", default="")
# Mock adapters are refused when DEBUG is off unless this is set explicitly (e.g. a staging demo).
ALLOW_MOCK_ADAPTERS = env.bool("ALLOW_MOCK_ADAPTERS", default=False)

# --- Business rules (ARCHITECTURE §15 open question 2/5: confirm values before go-live) ---
REPORT_PRICE_PAISE = env.int("REPORT_PRICE_PAISE", default=0)  # 0 = not configured; requests refuse to start
REPORT_VALIDITY_DAYS = env.int("REPORT_VALIDITY_DAYS", default=7)
REPORT_PDF_RETENTION_DAYS = env.int("REPORT_PDF_RETENTION_DAYS", default=30)
OTP_MAX_ATTEMPTS = env.int("OTP_MAX_ATTEMPTS", default=3)
OTP_MAX_RESENDS = env.int("OTP_MAX_RESENDS", default=2)
PAYMENT_LINK_EXPIRY_HOURS = env.int("PAYMENT_LINK_EXPIRY_HOURS", default=72)
FOLLOWUP_REMINDER_OFFSETS_HOURS = [
    int(h) for h in env.list("FOLLOWUP_REMINDER_OFFSETS_HOURS", default=["1", "24", "72"])
]
WHATSAPP_TEMPLATE_LANGUAGE = env("WHATSAPP_TEMPLATE_LANGUAGE", default="en")
# Logical name -> approved WhatsApp template name. Needed outside the 24-hour service window.
WHATSAPP_TEMPLATES = {
    "payment_link": env("WA_TEMPLATE_PAYMENT_LINK", default="payment_link_v1"),
    "payment_reminder": env("WA_TEMPLATE_PAYMENT_REMINDER", default="payment_reminder_v1"),
    "report_delivery": env("WA_TEMPLATE_REPORT_DELIVERY", default="report_delivery_v1"),
}

# --- Sessions / cookies (SPA uses session auth + CSRF, see ARCHITECTURE §6.6) ---
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_SAMESITE = "Lax"

# --- I18N / time: stored in UTC, displayed in IST ---
LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Private report storage: must never be under STATIC_ROOT / MEDIA_ROOT or any nginx-served path.
PRIVATE_STORAGE_ROOT = Path(env("PRIVATE_STORAGE_ROOT", default=str(REPO_ROOT / "private_reports")))

# --- DRF ---
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["apps.accounts.permissions.IsFullyAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_RATES": {"login": "10/min", "otp": "10/min"},
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}

# --- Redis / Celery ---
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")
# Shared cache: login lockout counters and DRF throttles must be shared across all web workers.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "cibil",
    }
}
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/2")
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_RESULT_EXPIRES = 60 * 60 * 24
CELERY_TASK_DEFAULT_QUEUE = "default"
# Queue names are declared in config/celery.py.
CELERY_TASK_ROUTES = {
    "apps.core.tasks.*": {"queue": "default"},
    "reports.initiate_otp": {"queue": "provider"},
    "reports.fetch_report": {"queue": "provider"},
    "reports.deliver_report": {"queue": "messaging"},
    "payments.*": {"queue": "payments"},
}
CELERY_BEAT_SCHEDULE: dict = {}

# --- Logging (JSON in prod, PII-scrubbed everywhere) ---
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOG_FORMAT = env("LOG_FORMAT", default="json")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "apps.core.logging.RequestIDFilter"},
        "pii": {"()": "apps.core.logging.PIIScrubFilter"},
    },
    "formatters": {
        "json": {"()": "apps.core.logging.JSONFormatter"},
        "console": {
            "format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
        },
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "formatter": LOG_FORMAT,
            "filters": ["request_id", "pii"],
        },
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        # Never log request bodies (webhooks carry PII/OTPs).
        "django.request": {"level": "WARNING"},
    },
}

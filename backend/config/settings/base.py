"""Settings shared by every environment. Environment-specific modules import from here."""

from pathlib import Path

import environ
from celery.schedules import crontab

from apps.messaging.templates import default_names as _wa_template_names

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
    "django_filters",
    "drf_spectacular",
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
# Surepass credit report API (CREDIT_PROVIDER=surepass). Token only ever from the environment.
# An empty value in .env falls back to the default rather than producing a blank URL.
SUREPASS_BASE_URL = env("SUREPASS_BASE_URL", default="") or "https://app.surepass.app"
SUREPASS_API_TOKEN = env("SUREPASS_API_TOKEN", default="")
# Prefill-by-mobile (name/PAN/gender from the customer's number): separate host; token defaults to the above.
SUREPASS_KYC_BASE_URL = env("SUREPASS_KYC_BASE_URL", default="") or "https://kyc-api.surepass.app"
SUREPASS_PREFILL_TOKEN = env("SUREPASS_PREFILL_TOKEN", default="")
# Use prefill when the provider supports it: the customer only confirms details instead of typing them.
PREFILL_BY_MOBILE = env.bool("PREFILL_BY_MOBILE", default=True)
PREFILL_MAX_PER_DAY = env.int("PREFILL_MAX_PER_DAY", default=3)  # paid lookups per WhatsApp number per day
# Hosts the report PDF may be downloaded from (the sample response links to an S3 bucket).
SUREPASS_PDF_HOSTS = env.list("SUREPASS_PDF_HOSTS", default=["amazonaws.com"])

# Razorpay (Phase 6). Test-mode keys start with rzp_test_. Secrets only ever come from the environment.
RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID", default="")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET", default="")
RAZORPAY_WEBHOOK_SECRET = env("RAZORPAY_WEBHOOK_SECRET", default="")
# WhatsApp Business Cloud API (Phase 7). Verify the Graph version against Meta's docs before go-live.
WHATSAPP_GRAPH_VERSION = env("WHATSAPP_GRAPH_VERSION", default="v21.0")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default="")
WHATSAPP_ACCESS_TOKEN = env("WHATSAPP_ACCESS_TOKEN", default="")
WHATSAPP_APP_SECRET = env("WHATSAPP_APP_SECRET", default="")  # signs inbound webhooks (X-Hub-Signature-256)
WHATSAPP_VERIFY_TOKEN = env("WHATSAPP_VERIFY_TOKEN", default="")  # echoed in the subscription handshake
# Seconds an inbound message body may wait in Redis for the worker (it may be an OTP: keep this short).
WHATSAPP_INBOUND_BODY_TTL = env.int("WHATSAPP_INBOUND_BODY_TTL", default=300)
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
# Follow-up automation (Phase 8)
REMINDER_DISPATCH_BATCH = env.int("REMINDER_DISPATCH_BATCH", default=100)
FOLLOWUP_ESCALATE_AFTER_HOURS = env.int("FOLLOWUP_ESCALATE_AFTER_HOURS", default=48)  # unpaid this long...
FOLLOWUP_ESCALATE_QUIET_HOURS = env.int(
    "FOLLOWUP_ESCALATE_QUIET_HOURS", default=24
)  # ...and no note this long
FOLLOWUP_MAX_ESCALATION = env.int("FOLLOWUP_MAX_ESCALATION", default=3)
# Providers without OTP don't prove PAN ownership: block the same PAN on another number / another PAN on the
# same number within this many days (0 disables; see ReportService._pan_guard).
PAN_GUARD_DAYS = env.int("PAN_GUARD_DAYS", default=30)
STALE_REQUEST_MINUTES = env.int("STALE_REQUEST_MINUTES", default=60)  # unfinished details/consent/OTP
WHATSAPP_TEMPLATE_LANGUAGE = env("WHATSAPP_TEMPLATE_LANGUAGE", default="en")
# Logical name -> approved WhatsApp template name. Needed outside the 24-hour service window.
# Wording, parameters and defaults live in apps/messaging/templates.py; each name can be overridden by its
# WA_TEMPLATE_* variable (payment_link, payment_reminder, report_delivery, report_expired).
WHATSAPP_TEMPLATES = _wa_template_names(env)

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
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.api.exception_handler",
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
    "payments.process_webhook_event": {"queue": "webhooks"},
    "conversations.*": {"queue": "webhooks"},
    "followups.*": {"queue": "reminders"},
    "messaging.*": {"queue": "webhooks"},
    "payments.*": {"queue": "payments"},
}
# Times are IST (CELERY_TIMEZONE). See ARCHITECTURE §11.
CELERY_BEAT_SCHEDULE: dict = {
    "dispatch-due-reminders": {"task": "followups.dispatch_due_reminders", "schedule": 60},
    "escalate-overdue-followups": {"task": "followups.escalate_overdue", "schedule": crontab(minute=5)},
    # Catch payments whose webhook never arrived (ARCHITECTURE §6.1 step 5).
    "reconcile-payment-links": {"task": "payments.reconcile_payment_links", "schedule": 10 * 60},
    # Reprocess webhook events that failed (e.g. DB briefly unavailable).
    "retry-failed-webhooks": {"task": "payments.retry_failed_webhook_events", "schedule": 5 * 60},
    "expire-stale-requests": {"task": "reports.expire_stale_requests", "schedule": 15 * 60},
    "purge-expired-documents": {
        "task": "reports.purge_expired_documents",
        "schedule": crontab(hour=2, minute=30),
    },
}

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

SPECTACULAR_SETTINGS = {
    "TITLE": "CIBIL WhatsApp Platform - Admin API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["apps.accounts.permissions.IsFullyAuthenticated"],
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "ReportStatusEnum": "apps.reports.models.ReportStatus",
        "DeliveryStatusEnum": "apps.reports.models.DeliveryStatus",
        "PaymentLinkStatusEnum": "apps.payments.models.PaymentLinkStatus",
        "PaymentStatusEnum": "apps.payments.models.PaymentStatus",
        "RefundStatusEnum": "apps.payments.models.RefundStatus",
        "FollowUpStatusEnum": "apps.followups.models.FollowUpStatus",
        "ReminderStatusEnum": "apps.followups.models.ReminderStatus",
        "MessageStatusEnum": "apps.messaging.models.MessageStatus",
    },
}

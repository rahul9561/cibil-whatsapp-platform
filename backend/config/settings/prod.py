"""Production settings. Everything secret or site-specific comes from the environment (see .env.example)."""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import MIDDLEWARE, REPORT_PRICE_PAISE, env

DEBUG = False

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must list the production domain(s).")
if REPORT_PRICE_PAISE <= 0:
    raise ImproperlyConfigured("REPORT_PRICE_PAISE must be set in production.")

# Behind nginx with TLS termination (nginx also redirects HTTP -> HTTPS).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SECURE_REDIRECT_EXEMPT = [r"^healthz$", r"^readyz$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("DJANGO_HSTS_SECONDS", default=60 * 60 * 24 * 365)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = env.bool("DJANGO_HSTS_PRELOAD", default=False)  # opt in deliberately: hard to undo
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=1)  # nginx

# Django admin/DRF static files are served by WhiteNoise from the app container (compressed, hashed names).
MIDDLEWARE = [MIDDLEWARE[0], MIDDLEWARE[1], "whitenoise.middleware.WhiteNoiseMiddleware", *MIDDLEWARE[2:]]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

LOG_FORMAT = "json"
LOGGING["handlers"]["stdout"]["formatter"] = "json"  # noqa: F405

# --- Error reporting (optional). PII must never leave the server: see _scrub below. ---
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    import sentry_sdk

    from apps.core.redaction import redact, scrub

    _SECRET_HEADERS = {
        "cookie",
        "authorization",
        "x-csrftoken",
        "x-hub-signature-256",
        "x-razorpay-signature",
    }

    def _scrub(event, hint):
        request = event.get("request") or {}
        for key in ("data", "cookies", "query_string", "env"):
            request.pop(key, None)
        headers = request.get("headers") or {}
        for key in list(headers):
            if key.lower() in _SECRET_HEADERS:
                headers.pop(key)
        event.pop("user", None)
        if "extra" in event:
            event["extra"] = redact(event["extra"])
        if event.get("message"):
            event["message"] = scrub(event["message"])
        for exc in (event.get("exception") or {}).get("values") or []:
            if exc.get("value"):
                exc["value"] = scrub(exc["value"])
            for frame in (exc.get("stacktrace") or {}).get("frames") or []:
                frame.pop("vars", None)  # local variables may hold PII or an OTP
        return event

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=env("SENTRY_ENVIRONMENT", default="production"),
        send_default_pii=False,
        include_local_variables=False,
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
        before_send=_scrub,
    )

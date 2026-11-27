"""Startup checks: refuse to run with missing or weak secrets."""

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=False)
def check_encryption_keys(app_configs, **kwargs):
    errors = []
    keys = getattr(settings, "FIELD_ENCRYPTION_KEYS", None) or []
    if not keys:
        errors.append(Error("FIELD_ENCRYPTION_KEYS is empty.", id="core.E001"))
    for i, key in enumerate(keys):
        try:
            Fernet(key)
        except (ValueError, TypeError):
            errors.append(Error(f"FIELD_ENCRYPTION_KEYS[{i}] is not a valid Fernet key.", id="core.E002"))

    blind = getattr(settings, "BLIND_INDEX_KEY", "") or ""
    if len(blind) < 32:
        errors.append(Error("BLIND_INDEX_KEY must be at least 32 characters.", id="core.E003"))
    if blind and blind in keys:
        errors.append(Error("BLIND_INDEX_KEY must differ from every field encryption key.", id="core.E004"))
    return errors


@register(Tags.security, deploy=False)
def check_report_file_keys(app_configs, **kwargs):
    from apps.reports.storage import StorageError, _keys

    _keys.cache_clear()
    try:
        _keys()
    except (StorageError, ValueError) as exc:
        return [Error(f"REPORT_FILE_ENCRYPTION_KEYS invalid: {exc}", id="core.E005")]
    return []


@register(Tags.security, deploy=False)
def check_mock_adapters(app_configs, **kwargs):
    if settings.DEBUG or getattr(settings, "ALLOW_MOCK_ADAPTERS", False):
        return []
    mocked = [
        name
        for name in ("CREDIT_PROVIDER", "PAYMENT_GATEWAY", "WHATSAPP_GATEWAY")
        if str(getattr(settings, name, "")).startswith("mock")
    ]
    if mocked:
        return [
            Error(
                f"Mock adapters configured with DEBUG off: {', '.join(mocked)}. "
                "Set ALLOW_MOCK_ADAPTERS=true only for a non-production demo.",
                id="core.E006",
            )
        ]
    return []


@register(Tags.security, deploy=False)
def check_razorpay(app_configs, **kwargs):
    if getattr(settings, "PAYMENT_GATEWAY", "") != "razorpay":
        return []
    errors = []
    for name in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
        if not getattr(settings, name, ""):
            errors.append(Error(f"{name} is required when PAYMENT_GATEWAY=razorpay.", id="core.E007"))
    key_id = getattr(settings, "RAZORPAY_KEY_ID", "")
    if key_id.startswith("rzp_live_") and settings.DEBUG:
        errors.append(Error("Live Razorpay keys must not be used with DEBUG on.", id="core.E008"))
    return errors


@register(Tags.security, deploy=False)
def check_whatsapp(app_configs, **kwargs):
    if getattr(settings, "WHATSAPP_GATEWAY", "") != "cloud_api":
        return []
    names = (
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_VERIFY_TOKEN",
    )
    return [
        Error(f"{name} is required when WHATSAPP_GATEWAY=cloud_api.", id="core.E009")
        for name in names
        if not getattr(settings, name, "")
    ]


@register(Tags.security, deploy=False)
def check_surepass(app_configs, **kwargs):
    report = getattr(settings, "CREDIT_PROVIDER", "") == "surepass"
    prefill = getattr(settings, "PREFILL_PROVIDER", "") == "surepass"
    if not (report or prefill):
        return []
    errors = []
    # Prefill may use its own token; it falls back to the main one (same rule as the adapter).
    token = (settings.SUREPASS_PREFILL_TOKEN if prefill else "") or settings.SUREPASS_API_TOKEN
    if not token:
        errors.append(
            Error(
                "SUREPASS_API_TOKEN is required when Surepass is the report or prefill provider.",
                id="core.E010",
            )
        )
    elif token.count(".") != 2 or token.endswith(">"):
        # A JWT has three dot-separated parts. Copying it from a terminal or editor that cuts long lines
        # (trailing ">") silently breaks every lookup with an authentication error (2026-11-26).
        errors.append(
            Warning(
                "SUREPASS token looks cut off (a complete token has 3 parts separated by dots).",
                hint="Copy it again with Surepass's Copy button and paste it on one line in .env.",
                id="core.W014",
            )
        )
    if report and not settings.SUREPASS_BASE_URL.startswith("https://"):
        errors.append(Error("SUREPASS_BASE_URL must use https.", id="core.E011"))
    if prefill and not settings.SUREPASS_KYC_BASE_URL.startswith("https://"):
        errors.append(Error("SUREPASS_KYC_BASE_URL must use https.", id="core.E015"))
    return errors


@register(Tags.security, deploy=False)
def check_smartauth(app_configs, **kwargs):
    if getattr(settings, "CREDIT_PROVIDER", "") != "smartauth":
        return []
    errors = []
    if not settings.BUREAU_B_AUTHKEY:
        errors.append(Error("BUREAU_B_AUTHKEY is required when CREDIT_PROVIDER=smartauth.", id="core.E012"))
    if not settings.BUREAU_B_BASE_URL.startswith("https://"):
        errors.append(Error("BUREAU_B_BASE_URL must be set and use https.", id="core.E013"))
    return errors

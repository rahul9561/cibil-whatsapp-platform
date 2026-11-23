"""Startup checks: refuse to run with missing or weak secrets."""

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.checks import Error, Tags, register


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
        if getattr(settings, name, "") == "mock"
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

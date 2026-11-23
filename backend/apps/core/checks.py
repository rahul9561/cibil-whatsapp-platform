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

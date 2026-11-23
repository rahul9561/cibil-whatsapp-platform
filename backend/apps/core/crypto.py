"""Field encryption and blind indexes (ARCHITECTURE §6.4).

- Encryption: MultiFernet (AES-128-CBC + HMAC-SHA256). The first key in FIELD_ENCRYPTION_KEYS encrypts;
  every key can decrypt, which allows rotation (`manage.py rotate_encryption_keys`).
- Blind index: HMAC-SHA256 with a separate key, so equal values can be looked up without decrypting.
  A `purpose` string is mixed in so the same value in two different fields gives unrelated hashes.
"""

import hashlib
import hmac
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.signals import setting_changed
from django.dispatch import receiver


class DecryptionError(Exception):
    """Ciphertext could not be decrypted with any configured key."""


@lru_cache(maxsize=1)
def get_fernet() -> MultiFernet:
    keys = settings.FIELD_ENCRYPTION_KEYS
    if not keys:
        raise RuntimeError("FIELD_ENCRYPTION_KEYS is not configured")
    return MultiFernet([Fernet(k) for k in keys])


@lru_cache(maxsize=1)
def _blind_index_key() -> bytes:
    key = settings.BLIND_INDEX_KEY
    if not key:
        raise RuntimeError("BLIND_INDEX_KEY is not configured")
    return key.encode()


@receiver(setting_changed)
def _reset_caches(*, setting, **kwargs):
    if setting in {"FIELD_ENCRYPTION_KEYS", "BLIND_INDEX_KEY"}:
        get_fernet.cache_clear()
        _blind_index_key.cache_clear()


def encrypt_str(plaintext: str) -> str:
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_str(token: str) -> str:
    try:
        return get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise DecryptionError("cannot decrypt field value") from exc


def rotate_token(token: str) -> str:
    """Re-encrypt a token under the newest key (no-op in meaning, new ciphertext)."""
    try:
        return get_fernet().rotate(token.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError) as exc:
        raise DecryptionError("cannot rotate field value") from exc


def blind_index(value: str, purpose: str) -> str:
    """Deterministic keyed hash for equality lookups. Normalise `value` before calling."""
    msg = f"{purpose}\x00{value}".encode()
    return hmac.new(_blind_index_key(), msg, hashlib.sha256).hexdigest()


# --- Normalisers: always apply the same one before hashing or comparing. ---

_NON_DIGITS = re.compile(r"\D")


def normalize_pan(value: str) -> str:
    return value.strip().upper()


def normalize_phone_in(value: str) -> str:
    """Indian mobile number to E.164 digits without '+': '919876543210'."""
    digits = _NON_DIGITS.sub("", value)
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    return digits


def normalize_email(value: str) -> str:
    return value.strip().lower()

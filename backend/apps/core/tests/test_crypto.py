import io

import pytest
from cryptography.fernet import Fernet
from django.core.checks import run_checks
from django.core.exceptions import FieldError
from django.core.management import call_command
from django.db import connection

from apps.core import crypto
from apps.core.crypto import (
    DecryptionError,
    blind_index,
    decrypt_str,
    encrypt_str,
    normalize_pan,
    normalize_phone_in,
)
from apps.core.tests.testapp.models import EncryptedSample

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


def raw_value(obj, column):
    """Read the stored (encrypted) column, bypassing the field's decryption."""
    table = obj._meta.db_table
    with connection.cursor() as cur:
        # UUIDs are stored as char(32) hex on both MySQL and SQLite.
        cur.execute(f"SELECT {column} FROM {table} WHERE id = %s", [obj.pk.hex])  # noqa: S608
        return cur.fetchone()[0]


# --- primitives ---------------------------------------------------------------------------------


def test_roundtrip_and_ciphertext_is_randomised():
    a, b = encrypt_str("ABCDE1234F"), encrypt_str("ABCDE1234F")
    assert a != b
    assert "ABCDE1234F" not in a
    assert decrypt_str(a) == decrypt_str(b) == "ABCDE1234F"


def test_unicode_roundtrip():
    assert decrypt_str(encrypt_str("राहुल शर्मा")) == "राहुल शर्मा"


def test_old_key_still_decrypts_after_rotation(settings):
    settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
    token = encrypt_str("secret")
    settings.FIELD_ENCRYPTION_KEYS = [KEY_B, KEY_A]  # new key prepended
    assert decrypt_str(token) == "secret"
    rotated = crypto.rotate_token(token)
    settings.FIELD_ENCRYPTION_KEYS = [KEY_B]  # old key removed
    assert decrypt_str(rotated) == "secret"
    with pytest.raises(DecryptionError):
        decrypt_str(token)


def test_garbage_and_wrong_key_raise_decryption_error(settings):
    with pytest.raises(DecryptionError):
        decrypt_str("not-a-token")
    settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
    token = encrypt_str("x")
    settings.FIELD_ENCRYPTION_KEYS = [KEY_B]
    with pytest.raises(DecryptionError):
        decrypt_str(token)


def test_blind_index_is_deterministic_keyed_and_purpose_separated(settings):
    h1 = blind_index("ABCDE1234F", "pan")
    assert h1 == blind_index("ABCDE1234F", "pan")
    assert h1 != blind_index("ABCDE1234F", "phone")
    assert len(h1) == 64 and "ABCDE" not in h1
    settings.BLIND_INDEX_KEY = "another-blind-index-key-0123456789abcdef"
    assert blind_index("ABCDE1234F", "pan") != h1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9876543210", "919876543210"),
        ("+91 98765 43210", "919876543210"),
        ("09876543210", "919876543210"),
        ("919876543210", "919876543210"),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone_in(raw) == expected


def test_normalize_pan():
    assert normalize_pan("  abcde1234f ") == "ABCDE1234F"


# --- model field --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_field_stores_ciphertext_and_loads_plaintext():
    obj = EncryptedSample.objects.create(secret="ABCDE1234F", other="hello")
    stored = raw_value(obj, "secret")
    assert stored != "ABCDE1234F" and "ABCDE1234F" not in stored
    assert decrypt_str(stored) == "ABCDE1234F"
    fresh = EncryptedSample.objects.get(pk=obj.pk)
    assert fresh.secret == "ABCDE1234F"
    assert fresh.other == "hello"


@pytest.mark.django_db
def test_field_null_and_empty_are_not_encrypted():
    obj = EncryptedSample.objects.create(secret=None, other="")
    assert raw_value(obj, "secret") is None
    assert raw_value(obj, "other") == ""
    assert EncryptedSample.objects.filter(secret__isnull=True).count() == 1


@pytest.mark.django_db
def test_equality_lookup_is_refused_but_blind_index_works():
    pan = normalize_pan("abcde1234f")
    EncryptedSample.objects.create(secret=pan, pan_hash=blind_index(pan, "pan"))
    with pytest.raises(FieldError, match="blind index"):
        EncryptedSample.objects.filter(secret="ABCDE1234F").count()
    assert EncryptedSample.objects.filter(pan_hash=blind_index("ABCDE1234F", "pan")).count() == 1


@pytest.mark.django_db
def test_rotate_command_reencrypts_all_rows_under_new_key(settings):
    settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
    objs = [EncryptedSample.objects.create(secret=f"value-{i}", other=f"o{i}") for i in range(5)]
    EncryptedSample.objects.create(secret=None)

    settings.FIELD_ENCRYPTION_KEYS = [KEY_B, KEY_A]
    out = io.StringIO()
    call_command("rotate_encryption_keys", "--batch-size", "2", stdout=out)
    assert "testapp.EncryptedSample: 6 rows" in out.getvalue()

    settings.FIELD_ENCRYPTION_KEYS = [KEY_B]  # old key retired
    for i, obj in enumerate(objs):
        fresh = EncryptedSample.objects.get(pk=obj.pk)
        assert fresh.secret == f"value-{i}"
        assert fresh.other == f"o{i}"
    assert EncryptedSample.objects.filter(secret__isnull=True).count() == 1


@pytest.mark.django_db
def test_rotate_dry_run_writes_nothing(settings):
    settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
    obj = EncryptedSample.objects.create(secret="v")
    before = raw_value(obj, "secret")
    settings.FIELD_ENCRYPTION_KEYS = [KEY_B, KEY_A]
    call_command("rotate_encryption_keys", "--dry-run", stdout=io.StringIO())
    assert raw_value(obj, "secret") == before


# --- startup checks -----------------------------------------------------------------------------


def _security_errors():
    return {e.id for e in run_checks(tags=["security"]) if e.id.startswith("core.")}


def test_checks_pass_with_valid_config():
    assert _security_errors() == set()


def test_checks_flag_bad_keys(settings):
    settings.FIELD_ENCRYPTION_KEYS = []
    settings.BLIND_INDEX_KEY = "short"
    assert {"core.E001", "core.E003"} <= _security_errors()
    settings.FIELD_ENCRYPTION_KEYS = ["not-a-fernet-key"]
    assert "core.E002" in _security_errors()
    settings.FIELD_ENCRYPTION_KEYS = [KEY_A]
    settings.BLIND_INDEX_KEY = KEY_A
    assert "core.E004" in _security_errors()

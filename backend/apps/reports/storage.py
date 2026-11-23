"""Private, encrypted storage for report PDFs (ARCHITECTURE §6.3).

File layout: <PRIVATE_STORAGE_ROOT>/<key[:2]>/<key>, where key is a random UUID hex (no customer data in
paths). Content is AES-256-GCM encrypted: b"RPT1" | key_id length (1 byte) | key_id | 12-byte nonce |
ciphertext+tag. The storage key is bound as associated data, so swapping two files on disk is detected.
"""

import base64
import contextlib
import hashlib
import os
import secrets
import tempfile
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.signals import setting_changed
from django.dispatch import receiver

MAGIC = b"RPT1"
_KEY_RE_OK = frozenset("0123456789abcdef")


class StorageError(Exception):
    pass


class IntegrityFailure(StorageError):
    """Ciphertext failed authentication or content hash mismatch: file tampered with or corrupted."""


@dataclass(frozen=True)
class StoredFile:
    storage_key: str
    sha256: str
    size_bytes: int
    key_id: str


@lru_cache(maxsize=1)
def _keys() -> tuple[str, dict[str, bytes]]:
    entries = settings.REPORT_FILE_ENCRYPTION_KEYS
    if not entries:
        raise StorageError("REPORT_FILE_ENCRYPTION_KEYS is not configured")
    keys: dict[str, bytes] = {}
    for entry in entries:
        key_id, _, b64 = entry.partition(":")
        raw = base64.b64decode(b64)
        if not key_id or len(raw) != 32:
            raise StorageError("each REPORT_FILE_ENCRYPTION_KEYS entry must be 'id:base64(32 bytes)'")
        keys[key_id] = raw
    current_id = entries[0].partition(":")[0]
    return current_id, keys


@receiver(setting_changed)
def _reset(*, setting, **kwargs):
    if setting in {"REPORT_FILE_ENCRYPTION_KEYS", "PRIVATE_STORAGE_ROOT"}:
        _keys.cache_clear()


def generate_key() -> str:
    """Helper for operators: a new key entry value (prefix with an id, e.g. 'k2:<value>')."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


class ReportStorage:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or settings.PRIVATE_STORAGE_ROOT)

    def _path(self, storage_key: str) -> Path:
        # Keys are always our own uuid4 hex; refuse anything else so a key can never escape the root.
        if len(storage_key) != 32 or not set(storage_key) <= _KEY_RE_OK:
            raise StorageError("invalid storage key")
        return self.root / storage_key[:2] / storage_key

    def save(self, content: bytes) -> StoredFile:
        key_id, keys = _keys()
        storage_key = uuid.uuid4().hex
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(keys[key_id]).encrypt(nonce, content, storage_key.encode())
        kid = key_id.encode()
        blob = MAGIC + bytes([len(kid)]) + kid + nonce + ciphertext

        path = self._path(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)  # atomic: readers never see a partial file
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return StoredFile(storage_key, hashlib.sha256(content).hexdigest(), len(content), key_id)

    def read(self, storage_key: str, expected_sha256: str | None = None) -> bytes:
        try:
            blob = self._path(storage_key).read_bytes()
        except FileNotFoundError as exc:
            raise StorageError("report file not found") from exc
        if not blob.startswith(MAGIC):
            raise IntegrityFailure("unknown file format")
        kid_len = blob[4]
        key_id = blob[5 : 5 + kid_len].decode()
        nonce = blob[5 + kid_len : 17 + kid_len]
        ciphertext = blob[17 + kid_len :]
        _, keys = _keys()
        if key_id not in keys:
            raise StorageError(f"encryption key {key_id!r} is no longer configured")
        try:
            content = AESGCM(keys[key_id]).decrypt(nonce, ciphertext, storage_key.encode())
        except InvalidTag as exc:
            raise IntegrityFailure("report file failed authentication") from exc
        if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
            raise IntegrityFailure("report content hash mismatch")
        return content

    def delete(self, storage_key: str) -> bool:
        try:
            self._path(storage_key).unlink()
            return True
        except FileNotFoundError:
            return False

import os

import pytest

from apps.reports.storage import IntegrityFailure, ReportStorage, StorageError

PDF = b"%PDF-1.4 secret report body"


@pytest.fixture
def storage(settings):
    return ReportStorage()


def test_roundtrip_and_ciphertext_on_disk(storage, settings):
    stored = storage.save(PDF)
    path = settings.PRIVATE_STORAGE_ROOT / stored.storage_key[:2] / stored.storage_key
    raw = path.read_bytes()
    assert b"secret report" not in raw and raw.startswith(b"RPT1")
    assert stored.size_bytes == len(PDF) and stored.key_id == "t1"
    assert storage.read(stored.storage_key, expected_sha256=stored.sha256) == PDF
    if os.name == "posix":
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_old_key_still_reads_after_rotation(storage, settings):
    stored = storage.save(PDF)
    settings.REPORT_FILE_ENCRYPTION_KEYS = [
        "t2:dGVzdC1maWxlLWtleS0yLTAwMDAwMDAwMDAwMDAwMDA=",
        *settings.REPORT_FILE_ENCRYPTION_KEYS,
    ]
    assert storage.read(stored.storage_key) == PDF
    assert storage.save(PDF).key_id == "t2"


def test_removed_key_is_reported(storage, settings):
    stored = storage.save(PDF)
    settings.REPORT_FILE_ENCRYPTION_KEYS = ["t9:dGVzdC1maWxlLWtleS05LTAwMDAwMDAwMDAwMDAwMDA="]
    with pytest.raises(StorageError, match="no longer configured"):
        storage.read(stored.storage_key)


def test_tampering_is_detected(storage, settings):
    stored = storage.save(PDF)
    path = settings.PRIVATE_STORAGE_ROOT / stored.storage_key[:2] / stored.storage_key
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0x01
    path.write_bytes(bytes(blob))
    with pytest.raises(IntegrityFailure):
        storage.read(stored.storage_key)


def test_swapped_files_are_detected(storage, settings):
    a, b = storage.save(PDF), storage.save(b"%PDF other")
    pa = settings.PRIVATE_STORAGE_ROOT / a.storage_key[:2] / a.storage_key
    pb = settings.PRIVATE_STORAGE_ROOT / b.storage_key[:2] / b.storage_key
    pa.write_bytes(pb.read_bytes())
    with pytest.raises(IntegrityFailure):
        storage.read(a.storage_key)


def test_hash_mismatch_is_detected(storage):
    stored = storage.save(PDF)
    with pytest.raises(IntegrityFailure):
        storage.read(stored.storage_key, expected_sha256="0" * 64)


@pytest.mark.parametrize("key", ["../../etc/passwd", "ab", "Z" * 32, "../" + "a" * 29])
def test_path_traversal_refused(storage, key):
    with pytest.raises(StorageError):
        storage.read(key)


def test_delete(storage):
    stored = storage.save(PDF)
    assert storage.delete(stored.storage_key) is True
    assert storage.delete(stored.storage_key) is False
    with pytest.raises(StorageError, match="not found"):
        storage.read(stored.storage_key)

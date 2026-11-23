import json
import logging

import pytest

from apps.core.logging import REDACTED, JSONFormatter, PIIScrubFilter, RequestIDFilter, scrub
from apps.core.middleware import request_id_var


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PAN is ABCDE1234F ok", "PAN is [PAN] ok"),
        ("call 9876543210 now", "call [PHONE] now"),
        ("call +91 9876543210 now", "call [PHONE] now"),
        ("wa 919876543210", "wa [PHONE]"),
        ("mail a.b+c@example.co.in", "mail [EMAIL]"),
        ("uid 1234 5678 9012", "uid [AADHAAR]"),
        ("order 12345 amount 49900", "order 12345 amount 49900"),  # ordinary numbers untouched
    ],
)
def test_scrub_patterns(raw, expected):
    assert scrub(raw) == expected


def _record(msg, *args, exc_info=None, **extra):
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, exc_info)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_filter_scrubs_formatted_args():
    record = _record("customer %s pan %s", "9876543210", "ABCDE1234F")
    PIIScrubFilter().filter(record)
    assert record.getMessage() == "customer [PHONE] pan [PAN]"


def test_filter_redacts_sensitive_extra_keys():
    record = _record("verifying", otp="482913", step="otp", note="from 9876543210")
    PIIScrubFilter().filter(record)
    assert record.otp == REDACTED
    assert record.step == "otp"
    assert record.note == "from [PHONE]"


def test_filter_scrubs_exception_text():
    try:
        raise ValueError("bad PAN ABCDE1234F")
    except ValueError:
        import sys

        record = _record("failed", exc_info=sys.exc_info())
    PIIScrubFilter().filter(record)
    assert "ABCDE1234F" not in record.exc_text
    assert "[PAN]" in record.exc_text


def test_json_formatter_output_is_scrubbed_and_has_request_id():
    token = request_id_var.set("req-123")
    try:
        record = _record("hello %s", "ABCDE1234F", otp="111111")
        RequestIDFilter().filter(record)
        PIIScrubFilter().filter(record)
        line = JSONFormatter().format(record)
    finally:
        request_id_var.reset(token)
    data = json.loads(line)
    assert data["msg"] == "hello [PAN]"
    assert data["request_id"] == "req-123"
    assert data["otp"] == REDACTED
    assert "111111" not in line

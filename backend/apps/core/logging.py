"""Logging helpers: request-id injection, PII scrubbing and a JSON formatter.

The scrubber is a safety net, not a licence to log customer data: code should still never pass
PII or OTPs to a logger. See ARCHITECTURE §6.5 / §6.9.
"""

import json
import logging
import re
from datetime import UTC, datetime

from .middleware import request_id_var

REDACTED = "[REDACTED]"

# Keys in `extra={...}` whose values are always dropped.
SENSITIVE_KEYS = frozenset(
    {
        "otp",
        "password",
        "pan",
        "dob",
        "date_of_birth",
        "aadhaar",
        "token",
        "access_token",
        "secret",
        "api_key",
        "authorization",
        "signature",
        "phone",
        "mobile",
        "email",
        "wa_id",
        "body",
    }
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    # Phone before Aadhaar: "91" + 10-digit mobile is also 12 digits.
    (re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)"), "[AADHAAR]"),
]

# Attributes every LogRecord has; anything else came from `extra=`.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "request_id",
}


def scrub(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _extra_keys(record: logging.LogRecord) -> list[str]:
    return [k for k in vars(record) if k not in _STANDARD_ATTRS]


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class PIIScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed args; keep the raw template only
            message = str(record.msg)
        record.msg = scrub(message)
        record.args = None

        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
            record.exc_info = None  # prevent formatters re-rendering the unscrubbed traceback

        for key in _extra_keys(record):
            value = getattr(record, key)
            if key.lower() in SENSITIVE_KEYS:
                setattr(record, key, REDACTED)
            elif isinstance(value, str):
                setattr(record, key, scrub(value))
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key in _extra_keys(record):
            payload[key] = getattr(record, key)
        if record.exc_text:
            payload["exc"] = record.exc_text
        elif record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)

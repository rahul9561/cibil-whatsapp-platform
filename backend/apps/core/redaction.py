"""Shared PII redaction rules, used by the logging filter and the audit log."""

import re
from typing import Any

REDACTED = "[REDACTED]"

# Dict keys whose values are always dropped, wherever they appear.
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
        "full_name",
        "address",
        "pincode",
    }
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    # Phone before Aadhaar: "91" + 10-digit mobile is also 12 digits.
    (re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)"), "[AADHAAR]"),
]


def scrub(text: str) -> str:
    """Replace PII-looking substrings in free text."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return k in SENSITIVE_KEYS or k.endswith(("_enc", "_password", "_secret", "_token"))


def redact(value: Any) -> Any:
    """Recursively redact sensitive keys and scrub strings in JSON-like data."""
    if isinstance(value, dict):
        return {k: (REDACTED if is_sensitive_key(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return scrub(value)
    return value

"""Input validation for the WhatsApp flow. Each returns the normalised value or None."""

import re
import unicodedata
from datetime import date

_PAN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_DATE_FORMATS = (
    re.compile(r"^(?P<d>\d{1,2})[-/. ](?P<m>\d{1,2})[-/. ](?P<y>\d{4})$"),
    re.compile(r"^(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})$"),
)
MIN_AGE, MAX_AGE = 18, 100


def clean_name(text: str) -> str | None:
    name = " ".join((text or "").split())
    if not 2 <= len(name) <= 100:
        return None

    # Letters plus combining marks: Indic scripts write vowel signs (e.g. ा, ्) as marks, not letters.
    def allowed(ch: str) -> bool:
        return ch.isalpha() or unicodedata.category(ch).startswith("M") or ch in " .'-"

    if not all(allowed(ch) for ch in name) or not any(ch.isalpha() for ch in name):
        return None
    return name


def clean_pan(text: str) -> str | None:
    pan = re.sub(r"\s+", "", text or "").upper()
    return pan if _PAN.match(pan) else None


def clean_dob(text: str, today: date | None = None) -> date | None:
    raw = (text or "").strip()
    for pattern in _DATE_FORMATS:
        m = pattern.match(raw)
        if not m:
            continue
        try:
            dob = date(int(m["y"]), int(m["m"]), int(m["d"]))
        except ValueError:
            return None
        today = today or date.today()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        return dob if MIN_AGE <= age <= MAX_AGE else None
    return None

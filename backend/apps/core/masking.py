"""Display masks for PII. The admin UI shows these by default; full values need `customer.reveal_pii`."""


def mask_pan(pan: str | None) -> str:
    if not pan:
        return ""
    pan = pan.strip().upper()
    return pan[:2] + "*" * (len(pan) - 4) + pan[-2:] if len(pan) > 4 else "*" * len(pan)


def mask_phone(digits: str | None) -> str:
    """'919876543210' -> '+91 98******10'."""
    if not digits:
        return ""
    cc, local = (digits[:-10], digits[-10:]) if len(digits) > 10 else ("", digits)
    masked = local[:2] + "*" * (len(local) - 4) + local[-2:] if len(local) > 4 else "*" * len(local)
    return f"+{cc} {masked}" if cc else masked


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def mask_name(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(part[:1] + "***" for part in name.split())

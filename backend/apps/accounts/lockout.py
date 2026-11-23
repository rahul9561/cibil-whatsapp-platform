"""Per-account login lockout, complementing the per-IP DRF throttle.

Counts failed password attempts per email (hashed into the cache key) inside a sliding window;
after LOGIN_LOCKOUT_THRESHOLD failures the account is locked for LOGIN_LOCKOUT_SECONDS.
"""

import hashlib

from django.conf import settings
from django.core.cache import cache


def _key(email: str) -> str:
    return "login-fail:" + hashlib.sha256(email.encode()).hexdigest()


def is_locked(email: str) -> bool:
    return cache.get(_key(email), 0) >= settings.LOGIN_LOCKOUT_THRESHOLD


def register_failure(email: str) -> int:
    key = _key(email)
    cache.add(key, 0, timeout=settings.LOGIN_LOCKOUT_SECONDS)
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, timeout=settings.LOGIN_LOCKOUT_SECONDS)
        count = 1
    if count >= settings.LOGIN_LOCKOUT_THRESHOLD:
        # Restart the window so the lock lasts the full period from the last failure.
        cache.set(key, count, timeout=settings.LOGIN_LOCKOUT_SECONDS)
    return count


def clear(email: str) -> None:
    cache.delete(_key(email))

"""Shared building blocks for external-service adapters (ARCHITECTURE §6.7, §9).

Every adapter raises one of two error families so callers decide uniformly:
- TransientError: timeouts, 5xx, 429, connection errors. Safe to retry with backoff.
- PermanentError: validation failures, rejected requests. Retrying will not help.
"""

import contextlib
import importlib
import random
import uuid

from django.conf import settings
from django.core.cache import cache


class IntegrationError(Exception):
    """Base for adapter errors. `code` is short and safe to store/log (never PII)."""

    def __init__(self, message: str = "", *, code: str = "integration_error"):
        super().__init__(message or code)
        self.code = code


class TransientError(IntegrationError):
    pass


class PermanentError(IntegrationError):
    pass


class NotConfiguredError(PermanentError):
    pass


def load_adapter(setting_name: str, registry: dict[str, str]):
    """Instantiate the adapter named by a setting, e.g. CREDIT_PROVIDER = "mock"."""
    name = getattr(settings, setting_name, "") or ""
    if name not in registry:
        raise NotConfiguredError(
            f"{setting_name}={name!r} is not a known adapter", code="adapter_not_configured"
        )
    module_path, _, cls_name = registry[name].rpartition(".")
    return getattr(importlib.import_module(module_path), cls_name)()


def backoff_seconds(retries: int, base: int = 10, cap: int = 600) -> int:
    """Exponential backoff with full jitter: 0..min(cap, base * 2^retries)."""
    return random.randint(0, min(cap, base * 2**retries))  # noqa: S311 (not crypto)


class LockNotAcquired(Exception):
    pass


@contextlib.contextmanager
def distributed_lock(name: str, timeout: int = 120):
    """Short-lived mutual exclusion across web/worker processes (Redis via the cache).

    Used around external calls that must not run twice concurrently for the same object,
    where holding a DB row lock for the duration of a network call would be wrong.
    """
    key, token = f"lock:{name}", uuid.uuid4().hex
    if not cache.add(key, token, timeout=timeout):
        raise LockNotAcquired(name)
    try:
        yield
    finally:
        if cache.get(key) == token:  # don't release a lock that expired and was re-taken
            cache.delete(key)

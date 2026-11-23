import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Throttle/lockout counters and mock-adapter state live in the cache; never leak between tests."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _isolated_storage(settings, tmp_path):
    """Each test writes report files to its own temporary private directory."""
    settings.PRIVATE_STORAGE_ROOT = tmp_path / "private_reports"


@pytest.fixture(autouse=True)
def _reset_mock_whatsapp():
    from apps.messaging.gateway.mock import MockWhatsAppGateway

    MockWhatsAppGateway.fail_next = None
    yield
    MockWhatsAppGateway.fail_next = None

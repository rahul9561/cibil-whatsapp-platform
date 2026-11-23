import pytest
from django.core.checks import run_checks

from apps.core.integrations import (
    LockNotAcquired,
    NotConfiguredError,
    backoff_seconds,
    distributed_lock,
    load_adapter,
)
from apps.reports.providers import get_provider
from apps.reports.providers.mock import MockCreditReportProvider


def test_lock_is_exclusive_and_released():
    with distributed_lock("x"), pytest.raises(LockNotAcquired), distributed_lock("x"):
        pass
    with distributed_lock("x"):
        pass


def test_lock_released_on_error():
    with pytest.raises(RuntimeError), distributed_lock("y"):
        raise RuntimeError
    with distributed_lock("y"):
        pass


def test_backoff_is_bounded():
    assert all(0 <= backoff_seconds(n) <= 600 for n in range(12))


def test_adapter_selection(settings):
    assert isinstance(get_provider(), MockCreditReportProvider)
    settings.CREDIT_PROVIDER = ""
    with pytest.raises(NotConfiguredError):
        get_provider()
    with pytest.raises(NotConfiguredError):
        load_adapter("CREDIT_PROVIDER", {"mock": "x.Y"})


def _errors():
    return {e.id for e in run_checks(tags=["security"]) if e.id.startswith("core.")}


def test_mock_adapters_refused_in_production(settings):
    settings.ALLOW_MOCK_ADAPTERS = False
    settings.DEBUG = False
    assert "core.E006" in _errors()
    settings.DEBUG = True
    assert "core.E006" not in _errors()


def test_bad_report_file_keys_flagged(settings):
    settings.REPORT_FILE_ENCRYPTION_KEYS = ["nokey"]
    assert "core.E005" in _errors()
    settings.REPORT_FILE_ENCRYPTION_KEYS = ["k1:dG9vLXNob3J0"]
    assert "core.E005" in _errors()


def test_tasks_are_routed_to_their_queues():
    from config.celery import app

    router = app.amqp.router
    expected = {
        "reports.initiate_otp": "provider",
        "reports.fetch_report": "provider",
        "reports.deliver_report": "messaging",
        "payments.create_payment_link": "payments",
        "payments.confirm_payment": "payments",
    }
    for name, queue in expected.items():
        assert router.route({}, name)["queue"].name == queue, name

from apps.core.integrations import load_adapter

from .base import CreditReportProvider

PROVIDERS = {
    "mock": "apps.reports.providers.mock.MockCreditReportProvider",
    # The authorised partner's adapter is registered here in Phase 10, from its documentation.
}


def get_provider() -> CreditReportProvider:
    return load_adapter("CREDIT_PROVIDER", PROVIDERS)

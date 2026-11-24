from apps.core.integrations import load_adapter

from .base import CreditReportProvider

PROVIDERS = {
    "mock": "apps.reports.providers.mock.MockCreditReportProvider",  # OTP flow
    "mock_no_otp": "apps.reports.providers.mock.MockDirectCreditReportProvider",  # Surepass-like, no OTP
    "surepass": "apps.reports.providers.surepass.SurepassCreditReportProvider",
}


def get_provider() -> CreditReportProvider:
    return load_adapter("CREDIT_PROVIDER", PROVIDERS)

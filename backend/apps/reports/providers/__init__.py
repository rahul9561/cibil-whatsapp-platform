from apps.core.integrations import load_adapter

from .base import CreditReportProvider

# The credit report itself (CREDIT_PROVIDER).
PROVIDERS = {
    "mock": "apps.reports.providers.mock.MockCreditReportProvider",  # OTP flow
    "mock_no_otp": "apps.reports.providers.mock.MockDirectCreditReportProvider",  # Surepass-like, no OTP
    "surepass": "apps.reports.providers.surepass.SurepassCreditReportProvider",
    "smartauth": "apps.reports.providers.smartauth.SmartAuthCreditReportProvider",  # PDF: befisc_reportforge
}

# Name/PAN/gender looked up from a mobile number (PREFILL_PROVIDER). A separate provider from the report:
# e.g. Surepass finds the details, SmartAuth then pulls the report with them.
PREFILL_PROVIDERS = {
    "surepass": "apps.reports.providers.surepass.SurepassCreditReportProvider",
    "mock": "apps.reports.providers.mock.MockDirectCreditReportProvider",
}


def get_provider() -> CreditReportProvider:
    return load_adapter("CREDIT_PROVIDER", PROVIDERS)


def get_prefill_provider() -> CreditReportProvider | None:
    """The provider that looks up details by mobile number, or None when there is none.

    PREFILL_PROVIDER picks it explicitly. Left empty, the report provider's own lookup is used if it has one
    (Surepass, the mocks); SmartAuth has none, so it needs PREFILL_PROVIDER=surepass.
    """
    from django.conf import settings

    if settings.PREFILL_PROVIDER:
        return load_adapter("PREFILL_PROVIDER", PREFILL_PROVIDERS)
    report = get_provider()
    return report if report.supports_prefill else None

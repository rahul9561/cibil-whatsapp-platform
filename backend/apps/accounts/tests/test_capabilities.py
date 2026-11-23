import pytest

from apps.accounts.capabilities import CAPABILITIES, capabilities_for, role_has
from apps.accounts.models import ROLES_REQUIRING_2FA, Role
from apps.accounts.permissions import require

READ_ONLY_PREFIXES = (".view", ".revenue")


def test_auditor_is_read_only():
    for cap in capabilities_for(Role.AUDITOR):
        assert cap.endswith(READ_ONLY_PREFIXES), cap


def test_refund_approval_is_separated_from_admin():
    assert role_has(Role.FINANCE, "refund.approve")
    assert not role_has(Role.ADMIN, "refund.approve")
    assert not role_has(Role.AGENT, "refund.request")


def test_only_super_admin_manages_staff_and_settings():
    for cap in ("staff.manage", "settings.manage"):
        assert CAPABILITIES[cap] == frozenset({Role.SUPER_ADMIN})


def test_pii_reveal_and_pdf_download_restricted():
    for cap in ("customer.reveal_pii", "report.download"):
        assert CAPABILITIES[cap] == frozenset({Role.SUPER_ADMIN, Role.ADMIN})


def test_roles_with_money_or_pii_powers_require_2fa():
    sensitive = {"customer.reveal_pii", "report.download", "refund.approve", "staff.manage"}
    for role in Role:
        if any(role_has(role, cap) for cap in sensitive):
            assert role in ROLES_REQUIRING_2FA, role


def test_unknown_capability_fails_fast():
    with pytest.raises(ValueError):
        require("refund.aprove")
    with pytest.raises(KeyError):
        role_has(Role.ADMIN, "nope")


@pytest.mark.django_db
def test_inactive_user_has_no_capabilities(django_user_model):
    user = django_user_model.objects.create_user(
        email="a@example.com", password="x" * 12, role=Role.SUPER_ADMIN, is_active=False
    )
    assert not user.has_capability("staff.manage")

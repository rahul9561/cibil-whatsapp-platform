"""Role → capability matrix (ARCHITECTURE §6.6). The API enforces these; the UI only mirrors them.

"Assigned only" restrictions for agents are object-level checks added with each endpoint (Phase 5).
"""

from .models import Role

SA, A, AG, FIN, AUD = Role.SUPER_ADMIN, Role.ADMIN, Role.AGENT, Role.FINANCE, Role.AUDITOR
ALL = frozenset(Role)

CAPABILITIES: dict[str, frozenset[str]] = {
    "dashboard.view": ALL,
    "customer.view": ALL,
    "customer.reveal_pii": frozenset({SA, A}),
    "report.view": ALL,
    "report.manage": frozenset({SA, A}),  # retry / cancel
    "report.download": frozenset({SA, A}),
    "payment.view": ALL,
    "payment.resend_link": frozenset({SA, A, AG}),
    "message.send": frozenset({SA, A, AG}),
    "followup.view": frozenset({SA, A, AG, AUD}),
    "followup.manage": frozenset({SA, A, AG}),  # notes, reminders, status
    "followup.assign": frozenset({SA, A}),
    "refund.request": frozenset({SA, A, FIN}),
    "refund.approve": frozenset({SA, FIN}),
    "analytics.revenue": frozenset({SA, A, FIN, AUD}),
    "staff.manage": frozenset({SA}),
    "audit.view": frozenset({SA, AUD}),
    "settings.manage": frozenset({SA}),
}


def role_has(role: str, capability: str) -> bool:
    try:
        return role in CAPABILITIES[capability]
    except KeyError:
        raise KeyError(f"Unknown capability: {capability}") from None


def capabilities_for(role: str) -> list[str]:
    return sorted(cap for cap, roles in CAPABILITIES.items() if role in roles)

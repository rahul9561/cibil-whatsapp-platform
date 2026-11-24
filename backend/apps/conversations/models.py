"""WhatsApp bot conversation state (ARCHITECTURE §5). One row per customer; the state machine logic
arrives in Phase 7. Message history lives in messaging.MessageLog."""

from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.core.models import UUIDModel
from apps.core.redaction import is_sensitive_key

SERVICE_WINDOW = timedelta(hours=24)


class ConversationState(models.TextChoices):
    NEW = "new", "New"
    ASK_NAME = "ask_name", "Asking name"
    ASK_PAN = "ask_pan", "Asking PAN"
    ASK_DOB = "ask_dob", "Asking date of birth"
    ASK_EXTRA = "ask_extra", "Asking provider-specific fields"
    CONFIRM_DETAILS = "confirm_details", "Confirming details"
    ASK_ALT_MOBILE = "ask_alt_mobile", "Asking an alternate mobile number"
    VERIFY_ALT_PAN = "verify_alt_pan", "Verifying PAN for an alternate number"
    AWAITING_CONSENT = "awaiting_consent", "Awaiting consent"
    OTP_REQUESTING = "otp_requesting", "Requesting OTP"
    AWAITING_OTP = "awaiting_otp", "Awaiting OTP"
    OTP_FAILED = "otp_failed", "OTP failed"
    FETCHING_REPORT = "fetching_report", "Fetching report"
    AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
    DELIVERED = "delivered", "Delivered"
    REPORT_FAILED = "report_failed", "Report failed"
    NO_HIT = "no_hit", "No credit history"
    ENDED = "ended", "Ended"


def _find_sensitive_keys(value, path="") -> list[str]:
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if is_sensitive_key(str(k)):
                found.append(here)
            found += _find_sensitive_keys(v, here)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += _find_sensitive_keys(v, f"{path}[{i}]")
    return found


class Conversation(UUIDModel):
    customer = models.OneToOneField(
        "customers.Customer", on_delete=models.PROTECT, related_name="conversation"
    )
    state = models.CharField(
        max_length=24, choices=ConversationState.choices, default=ConversationState.NEW, db_index=True
    )
    state_entered_at = models.DateTimeField(default=timezone.now)
    # Bot scratch space: references and counters only. PII goes on Customer (encrypted), never here.
    context = models.JSONField(default=dict, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True, db_index=True)
    active_report_request = models.ForeignKey(
        "reports.ReportRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    def __str__(self) -> str:
        return f"Conversation {self.customer_id} [{self.state}]"

    def save(self, *args, **kwargs):
        bad = _find_sensitive_keys(self.context)
        if bad:
            raise ValueError(f"Conversation.context must not hold PII; offending keys: {', '.join(bad)}")
        super().save(*args, **kwargs)

    def in_service_window(self, now=None) -> bool:
        """True while free-form messages are allowed (24 h after the customer's last message)."""
        if self.last_inbound_at is None:
            return False
        return (now or timezone.now()) - self.last_inbound_at < SERVICE_WINDOW

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.state_entered_at = timezone.now()

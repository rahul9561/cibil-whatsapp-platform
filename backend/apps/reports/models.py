"""Report requests and generated report documents (ARCHITECTURE §3, §6.2, §6.3).

Report status and delivery status are separate fields; payment state lives in the payments app and
follow-up state in the followups app.
"""

import secrets

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.fields import EncryptedTextField
from apps.core.models import UUIDModel


class InvalidTransition(Exception):
    pass


class ReportStatus(models.TextChoices):
    DRAFT = "draft", "Collecting details"
    CONSENTED = "consented", "Consent given"
    OTP_SENT = "otp_sent", "OTP sent"
    OTP_VERIFIED = "otp_verified", "OTP verified"
    FETCHING = "fetching", "Fetching report"
    GENERATED = "generated", "Report generated"
    FAILED = "failed", "Failed"
    NO_HIT = "no_hit", "No credit history"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"


class DeliveryStatus(models.TextChoices):
    NOT_DELIVERED = "not_delivered", "Not delivered"
    QUEUED = "queued", "Queued"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    READ = "read", "Read"
    FAILED = "failed", "Failed"


TERMINAL_REPORT_STATUSES = frozenset(
    {ReportStatus.FAILED, ReportStatus.NO_HIT, ReportStatus.EXPIRED, ReportStatus.CANCELLED}
)
# Handed to WhatsApp counts as done: delivery receipts can be lost (or never come if the customer blocks us),
# and a missing receipt must not lock the customer out of future requests. A later "failed" receipt still
# marks delivery FAILED for staff retry (retry does not depend on the request being open).
DELIVERED_STATUSES = frozenset({DeliveryStatus.SENT, DeliveryStatus.DELIVERED, DeliveryStatus.READ})

_ABANDON = {ReportStatus.CANCELLED, ReportStatus.EXPIRED, ReportStatus.FAILED}
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    ReportStatus.DRAFT: frozenset({ReportStatus.CONSENTED, *_ABANDON}),
    ReportStatus.CONSENTED: frozenset({ReportStatus.OTP_SENT, *_ABANDON}),
    ReportStatus.OTP_SENT: frozenset({ReportStatus.OTP_SENT, ReportStatus.OTP_VERIFIED, *_ABANDON}),  # resend
    ReportStatus.OTP_VERIFIED: frozenset({ReportStatus.FETCHING, *_ABANDON}),
    # No CANCELLED/EXPIRED while fetching: the provider call is in flight and may already be billed.
    ReportStatus.FETCHING: frozenset({ReportStatus.GENERATED, ReportStatus.FAILED, ReportStatus.NO_HIT}),
    ReportStatus.GENERATED: frozenset({ReportStatus.EXPIRED, ReportStatus.CANCELLED}),
    **{s: frozenset() for s in TERMINAL_REPORT_STATUSES},
}

# Crockford-style alphabet: no 0/O, 1/I/L confusion when customers read the reference out on a call.
_REF_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def new_public_ref() -> str:
    return "CR-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(8))


class ReportRequest(UUIDModel):
    public_ref = models.CharField(max_length=16, unique=True, default=new_public_ref, editable=False)
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="report_requests"
    )
    consent = models.OneToOneField(
        "customers.ConsentRecord",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="report_request",
    )

    report_status = models.CharField(
        max_length=20, choices=ReportStatus.choices, default=ReportStatus.DRAFT, db_index=True
    )
    delivery_status = models.CharField(
        max_length=20, choices=DeliveryStatus.choices, default=DeliveryStatus.NOT_DELIVERED, db_index=True
    )
    status_changed_at = models.DateTimeField(default=timezone.now)
    failure_code = models.CharField(max_length=64, blank=True)
    failure_detail = models.TextField(blank=True, help_text="Operator-facing; must not contain PII")

    provider_name = models.CharField(max_length=50, blank=True)
    provider_session_ref = models.CharField(max_length=128, blank=True, db_index=True)
    provider_report_ref = models.CharField(max_length=128, blank=True)

    # OTP bookkeeping only. The OTP itself is never stored anywhere (ARCHITECTURE §6.5).
    otp_attempts = models.PositiveSmallIntegerField(default=0)
    otp_resends = models.PositiveSmallIntegerField(default=0)
    otp_sent_at = models.DateTimeField(null=True, blank=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)

    price_paise = models.PositiveIntegerField(help_text="Price snapshot at creation, in paise")
    currency = models.CharField(max_length=3, default="INR")

    generated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Generated report can be sold until")
    delivered_at = models.DateTimeField(null=True, blank=True)

    # = customer_id while the request is open, NULL otherwise. The unique index allows only one open
    # request per customer (MySQL has no partial unique indexes; NULLs never collide). Maintained in save().
    active_lock = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["report_status", "delivery_status", "created_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(price_paise__gt=0), name="report_price_positive"),
            models.CheckConstraint(
                condition=Q(active_lock__isnull=True) | Q(active_lock=models.F("customer")),
                name="report_active_lock_is_customer",
            ),
        ]

    def __str__(self) -> str:
        return self.public_ref

    @property
    def is_open(self) -> bool:
        """Open = the customer's journey for this request is unfinished (blocks a new request)."""
        if self.report_status in TERMINAL_REPORT_STATUSES:
            return False
        if self.report_status == ReportStatus.GENERATED:
            return self.delivery_status not in DELIVERED_STATUSES
        return True

    def _sync_lock(self) -> None:
        self.active_lock = self.customer_id if self.is_open else None

    def save(self, *args, **kwargs):
        self._sync_lock()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "active_lock", "updated_at"}
        super().save(*args, **kwargs)

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in ALLOWED_TRANSITIONS[self.report_status]

    def transition_to(self, new_status: str, *, failure_code: str = "", failure_detail: str = "") -> None:
        """Validate and apply a report status change (caller saves, normally under select_for_update)."""
        if not self.can_transition_to(new_status):
            raise InvalidTransition(f"{self.public_ref}: {self.report_status} -> {new_status} not allowed")
        self.report_status = new_status
        self.status_changed_at = timezone.now()
        if new_status == ReportStatus.GENERATED:
            self.generated_at = self.status_changed_at
        if failure_code or failure_detail:
            self.failure_code, self.failure_detail = failure_code, failure_detail


class ReportDocument(UUIDModel):
    """Metadata for the encrypted PDF in private storage. The file itself never sits in a public path."""

    report_request = models.OneToOneField(ReportRequest, on_delete=models.PROTECT, related_name="document")
    storage_key = models.CharField(
        max_length=255, unique=True, help_text="Opaque path inside private storage"
    )
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    content_type = models.CharField(max_length=100, default="application/pdf")
    encryption_key_id = models.CharField(max_length=32)
    score = EncryptedTextField(blank=True, default="")
    wa_media_id = models.CharField(max_length=128, blank=True)
    wa_media_expires_at = models.DateTimeField(null=True, blank=True)
    purge_after = models.DateTimeField(null=True, blank=True, db_index=True)
    purged_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Document for {self.report_request_id}"

    @property
    def is_purged(self) -> bool:
        return self.purged_at is not None

"""Razorpay payment links, payments and refunds (ARCHITECTURE §3, §6.1). Amounts are integer paise."""

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.core.models import UUIDModel


class PaymentLinkStatus(models.TextChoices):
    PENDING = "pending", "Pending (not yet created at Razorpay)"
    ACTIVE = "active", "Active"
    PAID = "paid", "Paid"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Creation failed"


OPEN_LINK_STATUSES = frozenset({PaymentLinkStatus.PENDING, PaymentLinkStatus.ACTIVE})


class PaymentLink(UUIDModel):
    report_request = models.ForeignKey(
        "reports.ReportRequest", on_delete=models.PROTECT, related_name="payment_links"
    )
    attempt = models.PositiveSmallIntegerField()
    # Our idempotency key, sent to Razorpay as `reference_id` (max 40 chars there).
    reference_id = models.CharField(max_length=40, unique=True)
    razorpay_link_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    short_url = models.URLField(blank=True)
    amount_paise = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(
        max_length=20, choices=PaymentLinkStatus.choices, default=PaymentLinkStatus.PENDING, db_index=True
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    sent_count = models.PositiveSmallIntegerField(default=0)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Null when created by the system",
    )
    # = report_request_id while the link is open: at most one open link per request. Maintained in save().
    open_lock = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["report_request", "attempt"], name="paymentlink_unique_attempt"),
            models.CheckConstraint(condition=Q(amount_paise__gt=0), name="paymentlink_amount_positive"),
            models.CheckConstraint(
                condition=Q(open_lock__isnull=True) | Q(open_lock=F("report_request")),
                name="paymentlink_open_lock_is_request",
            ),
        ]

    def __str__(self) -> str:
        return self.reference_id

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_LINK_STATUSES

    def save(self, *args, **kwargs):
        self.open_lock = self.report_request_id if self.is_open else None
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "open_lock", "updated_at"}
        super().save(*args, **kwargs)


class PaymentStatus(models.TextChoices):
    AUTHORIZED = "authorized", "Authorized"
    CAPTURED = "captured", "Captured"
    FAILED = "failed", "Failed"
    PARTIALLY_REFUNDED = "partially_refunded", "Partially refunded"
    REFUNDED = "refunded", "Refunded"


class Payment(UUIDModel):
    payment_link = models.ForeignKey(PaymentLink, on_delete=models.PROTECT, related_name="payments")
    razorpay_payment_id = models.CharField(max_length=64, unique=True)
    razorpay_order_id = models.CharField(max_length=64, blank=True)
    amount_paise = models.PositiveIntegerField()
    amount_refunded_paise = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=3, default="INR")
    method = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, db_index=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_description = models.CharField(max_length=255, blank=True)
    raw = models.JSONField(default=dict, blank=True, help_text="Sanitised gateway payload")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_paise__gt=0), name="payment_amount_positive"),
            models.CheckConstraint(
                condition=Q(amount_refunded_paise__lte=F("amount_paise")),
                name="payment_refund_not_above_amount",
            ),
        ]

    def __str__(self) -> str:
        return self.razorpay_payment_id

    @property
    def refundable_paise(self) -> int:
        return self.amount_paise - self.amount_refunded_paise


class RefundStatus(models.TextChoices):
    REQUESTED = "requested", "Requested"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    PROCESSING = "processing", "Processing at Razorpay"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"


class Refund(UUIDModel):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    razorpay_refund_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    amount_paise = models.PositiveIntegerField()
    reason = models.TextField()
    status = models.CharField(
        max_length=20, choices=RefundStatus.choices, default=RefundStatus.REQUESTED, db_index=True
    )
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_paise__gt=0), name="refund_amount_positive"),
            # Four-eyes rule: nobody approves their own refund request.
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True) | ~Q(approved_by=F("requested_by")),
                name="refund_approver_not_requester",
            ),
        ]

    def __str__(self) -> str:
        return f"Refund {self.amount_paise}p ({self.status})"

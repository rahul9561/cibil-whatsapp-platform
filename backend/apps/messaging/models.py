"""WhatsApp message log (ARCHITECTURE §3, §5). Bodies are stored redacted; OTP messages as a placeholder."""

from django.conf import settings
from django.db import models

from apps.core.models import UUIDModel

OTP_PLACEHOLDER = "[OTP REDACTED]"


class Direction(models.TextChoices):
    INBOUND = "in", "Inbound"
    OUTBOUND = "out", "Outbound"


class MessageType(models.TextChoices):
    TEXT = "text", "Text"
    TEMPLATE = "template", "Template"
    INTERACTIVE = "interactive", "Interactive"
    DOCUMENT = "document", "Document"
    IMAGE = "image", "Image"
    OTHER = "other", "Other"


class MessageStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    QUEUED = "queued", "Queued"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    READ = "read", "Read"
    FAILED = "failed", "Failed"


class MessageLog(UUIDModel):
    customer = models.ForeignKey(
        "customers.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="messages"
    )
    direction = models.CharField(max_length=3, choices=Direction.choices)
    # Null until WhatsApp accepts an outbound message; NULLs don't collide in the unique index.
    wa_message_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    # Outbound de-duplication across Celery retries: the same logical send reuses the same key.
    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    type = models.CharField(max_length=16, choices=MessageType.choices, default=MessageType.TEXT)
    template_name = models.CharField(max_length=128, blank=True)
    body_redacted = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=MessageStatus.choices, db_index=True)
    error_code = models.CharField(max_length=32, blank=True)
    error_detail = models.CharField(max_length=255, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Staff member for manual messages; null for system/bot messages",
    )
    related_report_request = models.ForeignKey(
        "reports.ReportRequest", null=True, blank=True, on_delete=models.PROTECT, related_name="messages"
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["customer", "created_at"])]

    def __str__(self) -> str:
        return f"{self.direction} {self.type} {self.status}"

import uuid

from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class WebhookSource(models.TextChoices):
    RAZORPAY = "razorpay", "Razorpay"
    WHATSAPP = "whatsapp", "WhatsApp"


class WebhookProcessingStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    PROCESSING = "processing", "Processing"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"
    IGNORED = "ignored", "Ignored"


class WebhookEvent(models.Model):
    """Every inbound webhook, de-duplicated by (source, event_id) (ARCHITECTURE §6.1).

    `payload` is stored sanitised: message bodies and other PII are stripped before saving.
    """

    id = models.BigAutoField(primary_key=True)
    source = models.CharField(max_length=16, choices=WebhookSource.choices)
    event_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=100, db_index=True)
    signature_valid = models.BooleanField()
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processing_status = models.CharField(
        max_length=16, choices=WebhookProcessingStatus.choices, default=WebhookProcessingStatus.RECEIVED
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [models.UniqueConstraint(fields=["source", "event_id"], name="webhook_event_unique")]
        indexes = [models.Index(fields=["processing_status", "received_at"])]

    def __str__(self) -> str:
        return f"{self.source}:{self.event_type}:{self.event_id}"


class UUIDModel(TimeStampedModel):
    """Base for domain models: random UUID primary key (no guessable sequential ids) + timestamps."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True

"""Customers (WhatsApp users requesting reports) and their consent records.

All personal fields are encrypted at rest. Lookups go through blind indexes:
`wa_id_hash` (unique, one customer per WhatsApp number) and `pan_hash`.
"""

from django.db import models
from django.utils import timezone

from apps.core.crypto import blind_index, normalize_pan, normalize_phone_in
from apps.core.fields import EncryptedTextField
from apps.core.masking import mask_name, mask_pan, mask_phone
from apps.core.models import UUIDModel

WA_ID_PURPOSE = "customer.wa_id"
PAN_PURPOSE = "customer.pan"


class Language(models.TextChoices):
    ENGLISH = "en", "English"
    HINDI = "hi", "हिंदी (Hindi)"
    PUNJABI = "pa", "ਪੰਜਾਬੀ (Punjabi)"
    TELUGU = "te", "తెలుగు (Telugu)"
    TAMIL = "ta", "தமிழ் (Tamil)"


class Gender(models.TextChoices):
    MALE = "M", "Male"
    FEMALE = "F", "Female"
    OTHER = "O", "Other"


class CustomerQuerySet(models.QuerySet):
    def by_wa_id(self, raw_wa_id: str):
        return self.filter(wa_id_hash=blind_index(normalize_phone_in(raw_wa_id), WA_ID_PURPOSE))

    def by_pan(self, raw_pan: str):
        return self.filter(pan_hash=blind_index(normalize_pan(raw_pan), PAN_PURPOSE))


class Customer(UUIDModel):
    wa_id_hash = models.CharField(max_length=64, unique=True)
    wa_id = EncryptedTextField()
    full_name = EncryptedTextField(blank=True, default="")
    pan = EncryptedTextField(blank=True, default="")
    pan_hash = models.CharField(max_length=64, blank=True, db_index=True)
    dob = EncryptedTextField(blank=True, default="", help_text="ISO date, encrypted")
    email = EncryptedTextField(blank=True, default="")
    pincode = EncryptedTextField(blank=True, default="")
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True)
    # Chosen on first contact (empty = not asked yet); every bot message uses it.
    language = models.CharField(max_length=5, choices=Language.choices, blank=True)
    opted_out_at = models.DateTimeField(null=True, blank=True)
    anonymised_at = models.DateTimeField(null=True, blank=True)

    objects = CustomerQuerySet.as_manager()

    def __str__(self) -> str:
        return f"Customer {self.masked_phone}"

    # Always set searchable fields through these so the blind index stays in sync.
    def set_wa_id(self, raw: str) -> None:
        normalized = normalize_phone_in(raw)
        self.wa_id = normalized
        self.wa_id_hash = blind_index(normalized, WA_ID_PURPOSE)

    def set_pan(self, raw: str) -> None:
        normalized = normalize_pan(raw)
        self.pan = normalized
        self.pan_hash = blind_index(normalized, PAN_PURPOSE) if normalized else ""

    @property
    def masked_phone(self) -> str:
        return mask_phone(self.wa_id)

    @property
    def masked_pan(self) -> str:
        return mask_pan(self.pan)

    @property
    def masked_name(self) -> str:
        return mask_name(self.full_name)

    @property
    def is_opted_out(self) -> bool:
        return self.opted_out_at is not None


class ConsentRecord(UUIDModel):
    """Immutable evidence of consent. Only `withdrawn_at` may be set afterwards."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="consents")
    purpose = models.CharField(max_length=100)
    consent_text_version = models.CharField(max_length=32)
    consent_text_sha256 = models.CharField(max_length=64)
    wa_message_id = models.CharField(
        max_length=128, blank=True, help_text="Inbound message that gave consent"
    )
    given_at = models.DateTimeField(default=timezone.now)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-given_at"]

    def __str__(self) -> str:
        return f"Consent {self.consent_text_version} ({self.given_at:%Y-%m-%d})"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or set(update_fields) - {"withdrawn_at", "updated_at"}:
                raise ValueError("Consent records are immutable; only withdrawn_at can change.")
        super().save(*args, **kwargs)

    def withdraw(self) -> None:
        if self.withdrawn_at is None:
            self.withdrawn_at = timezone.now()
            self.save(update_fields=["withdrawn_at", "updated_at"])

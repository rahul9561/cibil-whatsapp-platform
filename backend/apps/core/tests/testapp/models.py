"""Test-only models (installed by config.settings.test only)."""

from django.db import models

from apps.core.fields import EncryptedTextField
from apps.core.models import UUIDModel


class EncryptedSample(UUIDModel):
    secret = EncryptedTextField(blank=True, null=True)
    other = EncryptedTextField(blank=True, default="")
    pan_hash = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        app_label = "testapp"

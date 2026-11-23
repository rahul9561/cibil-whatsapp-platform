"""Append-only audit log with a SHA-256 hash chain (ARCHITECTURE §6.8).

Each entry stores the previous entry's hash and its own hash over (its content + previous hash), so
editing or deleting any row breaks the chain from that point; `manage.py verify_audit_chain` finds it.
Writes are serialised through a single `AuditChainHead` row lock so concurrent writers cannot fork it.
"""

import hashlib
import json

from django.conf import settings
from django.db import models

GENESIS_HASH = "0" * 64


class AppendOnlyError(Exception):
    pass


class AuditLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError("Audit log entries cannot be updated.")

    def delete(self):
        raise AppendOnlyError("Audit log entries cannot be deleted.")

    def bulk_create(self, *args, **kwargs):
        raise AppendOnlyError("Use apps.audit.service.record() to write audit entries.")

    def bulk_update(self, *args, **kwargs):
        raise AppendOnlyError("Audit log entries cannot be updated.")


class ActorType(models.TextChoices):
    USER = "user", "User"
    SYSTEM = "system", "System"
    WEBHOOK = "webhook", "Webhook"
    CUSTOMER = "customer", "Customer"
    ANONYMOUS = "anonymous", "Anonymous"


class AuditLog(models.Model):
    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Snapshot so the entry stays meaningful (and hash-verifiable) if the user row changes.
    actor_id_snapshot = models.CharField(max_length=64, blank=True)
    actor_label = models.CharField(max_length=254, blank=True)
    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    action = models.CharField(max_length=100, db_index=True)
    object_type = models.CharField(max_length=100, blank=True, db_index=True)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    changes = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    request_id = models.CharField(max_length=64, blank=True, db_index=True)
    prev_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64, unique=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["object_type", "object_id", "created_at"])]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.actor_label or self.actor_type} {self.action}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("Audit log entries cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Audit log entries cannot be deleted.")

    def hash_payload(self) -> dict:
        return {
            "created_at": self.created_at.isoformat(),
            "actor_id": self.actor_id_snapshot,
            "actor_label": self.actor_label,
            "actor_type": self.actor_type,
            "action": self.action,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "changes": self.changes,
            "ip": self.ip or "",
            "user_agent": self.user_agent,
            "request_id": self.request_id,
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        canonical = json.dumps(self.hash_payload(), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditChainHead(models.Model):
    """Single row (pk=1) holding the latest hash; locked FOR UPDATE by every writer."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    last_hash = models.CharField(max_length=64, default=GENESIS_HASH)
    last_entry_id = models.BigIntegerField(null=True, blank=True)

    def __str__(self) -> str:
        return f"audit head #{self.last_entry_id}"

"""The only supported way to write audit entries.

    from apps.audit import service as audit
    audit.record("customer.pii_revealed", request=request, obj=customer, changes={"fields": ["pan"]})

`changes` is redacted (sensitive keys dropped, PII patterns scrubbed) before it is stored.
"""

from django.db import transaction
from django.utils import timezone

from apps.core.http import client_ip
from apps.core.middleware import request_id_var
from apps.core.redaction import redact

from .models import ActorType, AuditChainHead, AuditLog


def _actor_fields(actor, actor_type):
    if actor is not None and getattr(actor, "is_authenticated", False):
        return {
            "actor": actor,
            "actor_id_snapshot": str(actor.pk),
            "actor_label": getattr(actor, "email", str(actor))[:254],
            "actor_type": actor_type or ActorType.USER,
        }
    return {
        "actor": None,
        "actor_id_snapshot": "",
        "actor_label": "",
        "actor_type": actor_type or ActorType.SYSTEM,
    }


def _current_request_id(request) -> str:
    rid = getattr(request, "request_id", None) or request_id_var.get()
    return "" if rid == "-" else rid[:64]


def record(
    action: str,
    *,
    request=None,
    actor=None,
    actor_type: str | None = None,
    obj=None,
    object_type: str = "",
    object_id: str = "",
    changes: dict | None = None,
) -> AuditLog:
    if actor is None and request is not None:
        actor = getattr(request, "user", None)
        if actor_type is None and not getattr(actor, "is_authenticated", False):
            actor_type = ActorType.ANONYMOUS
    if obj is not None:
        object_type = object_type or obj._meta.label_lower
        object_id = object_id or str(obj.pk)

    entry = AuditLog(
        **_actor_fields(actor, actor_type),
        action=action,
        object_type=object_type,
        object_id=str(object_id),
        changes=redact(changes or {}),
        ip=client_ip(request) if request is not None else None,
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:255] if request is not None else ""),
        request_id=_current_request_id(request),
    )

    with transaction.atomic():
        head, _ = AuditChainHead.objects.select_for_update().get_or_create(pk=1)
        entry.created_at = timezone.now()
        entry.prev_hash = head.last_hash
        entry.hash = entry.compute_hash()
        entry.save()
        head.last_hash = entry.hash
        head.last_entry_id = entry.pk
        head.save(update_fields=["last_hash", "last_entry_id"])
    return entry


def verify_chain(batch_size: int = 1000) -> tuple[bool, int | None, int]:
    """Walk the chain oldest → newest. Returns (ok, first_bad_id, entries_checked)."""
    from .models import GENESIS_HASH

    expected_prev = GENESIS_HASH
    checked = 0
    for entry in AuditLog.objects.order_by("id").iterator(chunk_size=batch_size):
        if entry.prev_hash != expected_prev or entry.compute_hash() != entry.hash:
            return False, entry.pk, checked
        expected_prev = entry.hash
        checked += 1
    head = AuditChainHead.objects.filter(pk=1).first()
    if head and head.last_hash != expected_prev:
        # Newest entries were deleted (e.g. directly in SQL).
        return False, head.last_entry_id, checked
    return True, None, checked

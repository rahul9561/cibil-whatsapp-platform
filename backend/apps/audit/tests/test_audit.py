import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import RequestFactory

from apps.audit import service as audit
from apps.audit.models import GENESIS_HASH, ActorType, AppendOnlyError, AuditChainHead, AuditLog
from apps.core.redaction import REDACTED

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(email="admin@example.com", password="x" * 12)


def test_chain_links_entries_and_verifies(user):
    a = audit.record("one", actor=user)
    b = audit.record("two")
    c = audit.record("three", actor=user, object_type="x", object_id="1")
    assert a.prev_hash == GENESIS_HASH
    assert b.prev_hash == a.hash and c.prev_hash == b.hash
    assert AuditChainHead.objects.get(pk=1).last_hash == c.hash
    assert audit.verify_chain() == (True, None, 3)
    assert b.actor_type == ActorType.SYSTEM and a.actor_type == ActorType.USER
    assert a.actor_label == "admin@example.com"


def test_hash_survives_db_roundtrip(user):
    entry = audit.record("roundtrip", actor=user, changes={"n": 1, "nested": {"b": [1, 2], "a": None}})
    fresh = AuditLog.objects.get(pk=entry.pk)
    assert fresh.compute_hash() == entry.hash


def test_changes_are_redacted(user):
    entry = audit.record(
        "customer.updated",
        actor=user,
        changes={
            "pan": "ABCDE1234F",
            "note": "called 9876543210",
            "status": {"from": "a", "to": "b"},
            "full_name_enc": "gAAAA...",
        },
    )
    assert entry.changes["pan"] == REDACTED
    assert entry.changes["full_name_enc"] == REDACTED
    assert entry.changes["note"] == "called [PHONE]"
    assert entry.changes["status"] == {"from": "a", "to": "b"}


def test_request_context_is_captured(user, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    req = RequestFactory().post(
        "/x", HTTP_USER_AGENT="pytest-agent", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.9", REMOTE_ADDR="10.0.0.2"
    )
    req.user = user
    req.request_id = "abc123def456"
    entry = audit.record("thing.done", request=req, obj=user)
    assert (
        entry.ip == "10.0.0.9"
    )  # right-most XFF hop added by the trusted proxy; spoofable left part ignored
    assert entry.user_agent == "pytest-agent"
    assert entry.request_id == "abc123def456"
    assert entry.actor == user
    assert entry.object_type == "accounts.user" and entry.object_id == str(user.pk)


def test_orm_update_and_delete_are_blocked(user):
    entry = audit.record("x", actor=user)
    entry.action = "tampered"
    with pytest.raises(AppendOnlyError):
        entry.save()
    with pytest.raises(AppendOnlyError):
        entry.delete()
    with pytest.raises(AppendOnlyError):
        AuditLog.objects.filter(pk=entry.pk).update(action="tampered")
    with pytest.raises(AppendOnlyError):
        AuditLog.objects.all().delete()
    with pytest.raises(AppendOnlyError):
        AuditLog.objects.bulk_create([AuditLog(action="x")])


def test_sql_tampering_is_detected(user):
    audit.record("one", actor=user)
    second = audit.record("two", actor=user, changes={"amount": 100})
    audit.record("three", actor=user)
    with connection.cursor() as cur:
        cur.execute("UPDATE audit_auditlog SET action = %s WHERE id = %s", ["edited", second.pk])
    ok, bad_id, checked = audit.verify_chain()
    assert (ok, bad_id, checked) == (False, second.pk, 1)
    with pytest.raises(CommandError, match=f"id={second.pk}"):
        call_command("verify_audit_chain")


def test_deleting_newest_entry_is_detected(user):
    audit.record("one", actor=user)
    last = audit.record("two", actor=user)
    with connection.cursor() as cur:
        cur.execute("DELETE FROM audit_auditlog WHERE id = %s", [last.pk])
    ok, bad_id, _ = audit.verify_chain()
    assert not ok and bad_id == last.pk


def test_deleting_middle_entry_is_detected(user):
    audit.record("one", actor=user)
    middle = audit.record("two", actor=user)
    third = audit.record("three", actor=user)
    with connection.cursor() as cur:
        cur.execute("DELETE FROM audit_auditlog WHERE id = %s", [middle.pk])
    ok, bad_id, _ = audit.verify_chain()
    assert not ok and bad_id == third.pk


def test_verify_command_ok(user):
    audit.record("one", actor=user)
    out = io.StringIO()
    call_command("verify_audit_chain", stdout=out)
    assert "Audit chain OK (1 entries)" in out.getvalue()

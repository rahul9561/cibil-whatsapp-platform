from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.conversations.models import Conversation, ConversationState
from apps.core.models import WebhookEvent, WebhookSource
from apps.core.testing.factories import CustomerFactory
from apps.messaging.models import Direction, MessageLog, MessageStatus

pytestmark = pytest.mark.django_db


def test_one_conversation_per_customer():
    c = CustomerFactory()
    Conversation.objects.create(customer=c)
    with pytest.raises(IntegrityError), transaction.atomic():
        Conversation.objects.create(customer=c)


@pytest.mark.parametrize(
    "context", [{"pan": "ABCDE1234F"}, {"draft": {"full_name": "x"}}, {"steps": [{"otp": "123456"}]}]
)
def test_context_rejects_pii_keys(context):
    with pytest.raises(ValueError, match="must not hold PII"):
        Conversation.objects.create(customer=CustomerFactory(), context=context)


def test_context_allows_references_and_counters():
    conv = Conversation.objects.create(customer=CustomerFactory(), context={"retries": 1, "step": "ask_pan"})
    assert conv.context["retries"] == 1


def test_service_window():
    conv = Conversation(customer=CustomerFactory())
    assert not conv.in_service_window()
    now = timezone.now()
    conv.last_inbound_at = now - timedelta(hours=23, minutes=59)
    assert conv.in_service_window(now)
    conv.last_inbound_at = now - timedelta(hours=24)
    assert not conv.in_service_window(now)


def test_set_state_tracks_entry_time():
    conv = Conversation(customer=CustomerFactory())
    before = conv.state_entered_at
    conv.set_state(ConversationState.ASK_NAME)
    assert conv.state == ConversationState.ASK_NAME and conv.state_entered_at >= before


def test_message_ids_unique_but_nullable():
    kw = {"direction": Direction.OUTBOUND, "status": MessageStatus.QUEUED}
    MessageLog.objects.create(**kw)
    MessageLog.objects.create(**kw)  # both still without a WhatsApp id
    MessageLog.objects.create(wa_message_id="wamid.A", idempotency_key="k1", **kw)
    with pytest.raises(IntegrityError), transaction.atomic():
        MessageLog.objects.create(wa_message_id="wamid.A", **kw)
    with pytest.raises(IntegrityError), transaction.atomic():
        MessageLog.objects.create(idempotency_key="k1", **kw)


def test_webhook_events_deduplicated_per_source():
    kw = {"event_type": "payment_link.paid", "signature_valid": True}
    WebhookEvent.objects.create(source=WebhookSource.RAZORPAY, event_id="evt_1", **kw)
    WebhookEvent.objects.create(source=WebhookSource.WHATSAPP, event_id="evt_1", **kw)  # other source: fine
    with pytest.raises(IntegrityError), transaction.atomic():
        WebhookEvent.objects.create(source=WebhookSource.RAZORPAY, event_id="evt_1", **kw)

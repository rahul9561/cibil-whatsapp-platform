from datetime import timedelta

import pytest
from django.utils import timezone

from apps.conversations.models import Conversation
from apps.core.integrations import TransientError
from apps.core.testing.factories import CustomerFactory
from apps.messaging.gateway.base import OutsideServiceWindow
from apps.messaging.gateway.mock import MockWhatsAppGateway, outbox
from apps.messaging.models import MessageLog, MessageStatus, MessageType
from apps.messaging.services import CustomerOptedOut, MessagingService

pytestmark = pytest.mark.django_db


def customer(last_inbound_hours_ago=None, **kw):
    c = CustomerFactory(**kw)
    last = (
        None if last_inbound_hours_ago is None else timezone.now() - timedelta(hours=last_inbound_hours_ago)
    )
    Conversation.objects.create(customer=c, last_inbound_at=last)
    return c


def test_free_text_inside_window():
    c = customer(last_inbound_hours_ago=1)
    row = MessagingService().send(c, text="Hello", template_key="payment_link", template_params=["x"])
    assert row.type == MessageType.TEXT and row.status == MessageStatus.SENT and row.wa_message_id
    assert outbox()[-1]["kind"] == "text"


def test_template_outside_window(settings):
    c = customer(last_inbound_hours_ago=25)
    row = MessagingService().send(
        c, text="Hello", template_key="payment_link", template_params=["CR-1", "₹99"]
    )
    assert (
        row.type == MessageType.TEMPLATE and row.template_name == settings.WHATSAPP_TEMPLATES["payment_link"]
    )
    assert outbox()[-1]["params"] == ["CR-1", "₹99"]


def test_free_text_outside_window_without_template_is_refused():
    with pytest.raises(OutsideServiceWindow):
        MessagingService().send(customer(), text="Hello")
    assert not MessageLog.objects.exists()


def test_reminders_respect_opt_out_but_transactional_messages_do_not():
    c = customer(last_inbound_hours_ago=1)
    c.opted_out_at = timezone.now()
    c.save()
    with pytest.raises(CustomerOptedOut):
        MessagingService().send(c, text="Reminder", is_reminder=True)
    MessagingService().send(c, text="Your report")  # e.g. delivery of a paid report


def test_idempotency_key_prevents_double_send():
    c = customer(last_inbound_hours_ago=1)
    svc = MessagingService()
    first = svc.send(c, text="Hi", idempotency_key="k1")
    second = svc.send(c, text="Hi", idempotency_key="k1")
    assert first.pk == second.pk and len(outbox()) == 1


def test_transient_failure_keeps_row_queued_and_retry_reuses_it():
    c = customer(last_inbound_hours_ago=1)
    svc = MessagingService()
    MockWhatsAppGateway.fail_next = TransientError("timeout", code="timeout")
    with pytest.raises(TransientError):
        svc.send(c, text="Hi", idempotency_key="k2")
    row = MessageLog.objects.get(idempotency_key="k2")
    assert row.status == MessageStatus.QUEUED and not row.wa_message_id
    svc.send(c, text="Hi", idempotency_key="k2")
    row.refresh_from_db()
    assert row.status == MessageStatus.SENT and MessageLog.objects.count() == 1


def test_permanent_failure_is_recorded():
    c = customer(last_inbound_hours_ago=1, wa_id="919800000000")  # mock: numbers ending 0000 are invalid
    with pytest.raises(Exception, match="not a WhatsApp user"):
        MessagingService().send(c, text="Hi")
    row = MessageLog.objects.get()
    assert row.status == MessageStatus.FAILED and row.error_code == "recipient_invalid"


def test_body_is_stored_redacted():
    c = customer(last_inbound_hours_ago=1)
    row = MessagingService().send(c, text="PAN ABCDE1234F, call 9876543210")
    assert row.body_redacted == "PAN [PAN], call [PHONE]"

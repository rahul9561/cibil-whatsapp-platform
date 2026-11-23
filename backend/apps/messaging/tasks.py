from celery import shared_task
from django.utils import timezone

from apps.core.models import WebhookEvent, WebhookProcessingStatus
from apps.core.tasks import run_with_retries

from .inbound import StatusUpdate, apply_status


@shared_task(bind=True, max_retries=5, name="messaging.process_whatsapp_event")
def process_whatsapp_event_task(self, event_pk: int):
    """Arguments are an id only: message text never travels through the broker."""
    event = WebhookEvent.objects.get(pk=event_pk)
    WebhookEvent.objects.filter(pk=event_pk).update(
        processing_status=WebhookProcessingStatus.PROCESSING, attempts=event.attempts + 1
    )

    def run():
        if event.event_type.startswith("status."):
            p = event.payload
            from datetime import datetime

            msg = apply_status(
                StatusUpdate(
                    p["wa_message_id"],
                    p["status"],
                    datetime.fromisoformat(p["timestamp"]),
                    p.get("error_code", ""),
                )
            )
            outcome = WebhookProcessingStatus.PROCESSED if msg else WebhookProcessingStatus.IGNORED
        else:
            from apps.conversations.bot import process_inbound_event

            process_inbound_event(event)
            outcome = WebhookProcessingStatus.PROCESSED
        WebhookEvent.objects.filter(pk=event_pk).update(
            processing_status=outcome, processed_at=timezone.now(), last_error=""
        )
        return outcome

    def give_up(exc):
        WebhookEvent.objects.filter(pk=event_pk).update(
            processing_status=WebhookProcessingStatus.FAILED, last_error=exc.code
        )

    return run_with_retries(self, run, on_give_up=give_up)

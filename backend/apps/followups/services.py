"""Follow-up lifecycle for generated-but-unpaid reports (ARCHITECTURE §7). Reminder dispatch is Phase 8."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit import service as audit
from apps.audit.models import ActorType

from .models import FollowUp, FollowUpStatus, Reminder, ReminderKind, ReminderStatus


class FollowUpService:
    def open_for(self, report_request) -> FollowUp:
        """Create the follow-up and its automatic reminder schedule (idempotent)."""
        with transaction.atomic():
            followup, created = FollowUp.objects.get_or_create(report_request=report_request)
            if not created:
                return followup
            now = timezone.now()
            times = [now + timedelta(hours=h) for h in settings.FOLLOWUP_REMINDER_OFFSETS_HOURS]
            Reminder.objects.bulk_create(
                Reminder(
                    follow_up=followup,
                    scheduled_for=t,
                    kind=ReminderKind.AUTO,
                    template_name=settings.WHATSAPP_TEMPLATES["payment_reminder"],
                )
                for t in times
            )
            followup.next_reminder_at = min(times) if times else None
            followup.save(update_fields=["next_reminder_at", "updated_at"])
            audit.record(
                "followup.opened",
                actor_type=ActorType.SYSTEM,
                obj=followup,
                changes={"reminders": len(times)},
            )
        return followup

    def close_paid(self, report_request) -> FollowUp | None:
        """Close automatically after a verified payment and cancel pending reminders."""
        with transaction.atomic():
            followup = FollowUp.objects.select_for_update().filter(report_request=report_request).first()
            if followup is None or not followup.is_open:
                return followup
            previous = followup.status
            followup.status = FollowUpStatus.CLOSED_PAID
            followup.closed_at = timezone.now()
            followup.close_reason = "Payment verified"
            followup.next_reminder_at = None
            followup.save()
            cancelled = followup.reminders.filter(status=ReminderStatus.SCHEDULED).update(
                status=ReminderStatus.CANCELLED, skip_reason="paid", updated_at=timezone.now()
            )
            audit.record(
                "followup.closed",
                actor_type=ActorType.SYSTEM,
                obj=followup,
                changes={"from": previous, "to": followup.status, "reminders_cancelled": cancelled},
            )
        return followup

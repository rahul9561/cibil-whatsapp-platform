"""Reminder dispatch and escalation (ARCHITECTURE §7, §11).

Dispatch is two-step so a reminder is sent at most once even with several workers or overlapping beats:
1. `dispatch_due_reminders` (every minute) claims due rows with SELECT ... FOR UPDATE SKIP LOCKED and
   marks them QUEUED in one transaction, then enqueues one task per reminder.
2. `send_reminder` re-checks every stop condition under a row lock, then sends with an idempotency key.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.audit import service as audit
from apps.audit.models import ActorType
from apps.core.integrations import PermanentError
from apps.core.tasks import run_with_retries
from apps.payments.models import PaymentLinkStatus
from apps.reports.models import ReportStatus

from .models import OPEN_FOLLOWUP_STATUSES, FollowUp, FollowUpStatus, Priority, Reminder, ReminderStatus

logger = logging.getLogger(__name__)
STUCK_AFTER = timedelta(minutes=15)


def _refresh_next(followup_id) -> None:
    next_at = (
        Reminder.objects.filter(follow_up_id=followup_id, status=ReminderStatus.SCHEDULED)
        .order_by("scheduled_for")
        .values_list("scheduled_for", flat=True)
        .first()
    )
    FollowUp.objects.filter(pk=followup_id).update(next_reminder_at=next_at)


@shared_task(name="followups.dispatch_due_reminders")
def dispatch_due_reminders() -> int:
    now = timezone.now()
    open_values = [s.value for s in OPEN_FOLLOWUP_STATUSES]
    with transaction.atomic():
        due = list(
            Reminder.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status=ReminderStatus.SCHEDULED, scheduled_for__lte=now)
                | Q(status=ReminderStatus.QUEUED, updated_at__lt=now - STUCK_AFTER)
            )  # worker died mid-send
            .filter(follow_up__status__in=open_values)
            .order_by("scheduled_for")
            .values_list("pk", flat=True)[: settings.REMINDER_DISPATCH_BATCH]
        )
        Reminder.objects.filter(pk__in=due).update(status=ReminderStatus.QUEUED, updated_at=now)
    for pk in due:  # the claim above is committed, so workers will see QUEUED rows
        send_reminder_task.delay(str(pk))
    return len(due)


def _skip(reminder: Reminder, reason: str) -> str:
    reminder.status, reminder.skip_reason = ReminderStatus.SKIPPED, reason
    reminder.save(update_fields=["status", "skip_reason", "updated_at"])
    _refresh_next(reminder.follow_up_id)
    return f"skipped:{reason}"


def send_reminder(reminder_id) -> str:
    from apps.payments.services import PaymentService

    with transaction.atomic():
        reminder = (
            Reminder.objects.select_for_update()
            .select_related("follow_up__report_request__customer")
            .get(pk=reminder_id)
        )
        if reminder.status != ReminderStatus.QUEUED:
            return "not_queued"  # already handled by another worker
        followup = reminder.follow_up
        req = followup.report_request
        if not followup.is_open:
            return _skip(reminder, "followup_closed")
        if req.payment_links.filter(status=PaymentLinkStatus.PAID).exists():
            return _skip(reminder, "paid")
        if req.customer.is_opted_out:
            return _skip(reminder, "opted_out")
        if req.report_status != ReportStatus.GENERATED or (
            req.expires_at and req.expires_at <= timezone.now()
        ):
            return _skip(reminder, "report_not_payable")

    service = PaymentService()
    try:
        link = service.get_or_create_link(req.pk)  # replaces an expiring link instead of sending a dead one
        msg = service.send_link(link, is_reminder=True)
    except PermanentError as exc:
        Reminder.objects.filter(pk=reminder.pk).update(
            status=ReminderStatus.FAILED, skip_reason=exc.code[:255]
        )
        _refresh_next(reminder.follow_up_id)
        audit.record(
            "followup.reminder_failed",
            actor_type=ActorType.SYSTEM,
            obj=followup,
            changes={"reminder": str(reminder.pk), "code": exc.code},
        )
        return f"failed:{exc.code}"
    Reminder.objects.filter(pk=reminder.pk).update(
        status=ReminderStatus.SENT, sent_message=msg, updated_at=timezone.now()
    )
    _refresh_next(reminder.follow_up_id)
    audit.record(
        "followup.reminder_sent",
        actor_type=ActorType.SYSTEM,
        obj=followup,
        changes={"reminder": str(reminder.pk), "kind": reminder.kind, "message_type": msg.type},
    )
    return "sent"


@shared_task(bind=True, max_retries=5, name="followups.send_reminder")
def send_reminder_task(self, reminder_id: str):
    # Transient failures leave the reminder QUEUED; the retry (or the stuck-row sweep) resends with the
    # same idempotency key, so the customer never gets it twice.
    return run_with_retries(self, lambda: send_reminder(reminder_id))


@shared_task(name="followups.escalate_overdue")
def escalate_overdue_followups() -> int:
    """Escalate unpaid follow-ups nobody has worked on recently, one level per quiet period (max N)."""
    now = timezone.now()
    quiet = now - timedelta(hours=settings.FOLLOWUP_ESCALATE_QUIET_HOURS)
    overdue_since = now - timedelta(hours=settings.FOLLOWUP_ESCALATE_AFTER_HOURS)
    candidates = (
        FollowUp.objects.filter(
            status__in=[s.value for s in OPEN_FOLLOWUP_STATUSES],
            escalation_level__lt=settings.FOLLOWUP_MAX_ESCALATION,
            report_request__generated_at__lte=overdue_since,
        )
        .exclude(report_request__payment_links__status=PaymentLinkStatus.PAID)
        .annotate(last_note_at=Max("notes__created_at"))
        .filter(Q(last_note_at__isnull=True) | Q(last_note_at__lt=quiet))
        .filter(Q(escalated_at__isnull=True) | Q(escalated_at__lt=quiet))
    )
    count = 0
    for fu in candidates:
        with transaction.atomic():
            locked = FollowUp.objects.select_for_update().get(pk=fu.pk)
            if not locked.is_open:
                continue
            locked.status = FollowUpStatus.ESCALATED
            locked.escalation_level += 1
            locked.escalated_at = now
            locked.priority = Priority.HIGH
            locked.save()
            audit.record(
                "followup.escalated",
                actor_type=ActorType.SYSTEM,
                obj=locked,
                changes={
                    "level": locked.escalation_level,
                    "assigned_to": str(locked.assigned_to_id or "unassigned"),
                },
            )
        count += 1
    return count

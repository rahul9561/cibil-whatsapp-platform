"""Follow-up of generated-but-unpaid reports (ARCHITECTURE §7).

Follow-up status is independent of report and payment status.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import UUIDModel


class FollowUpStatus(models.TextChoices):
    OPEN = "open", "Open"
    IN_PROGRESS = "in_progress", "In progress"
    ESCALATED = "escalated", "Escalated"
    CLOSED_PAID = "closed_paid", "Closed — paid"
    CLOSED_LOST = "closed_lost", "Closed — lost"
    CLOSED_MANUAL = "closed_manual", "Closed — manually"


OPEN_FOLLOWUP_STATUSES = frozenset(
    {FollowUpStatus.OPEN, FollowUpStatus.IN_PROGRESS, FollowUpStatus.ESCALATED}
)


# Sorted: a frozenset's order varies between runs and would make the migration non-deterministic.
_OPEN_VALUES = sorted(s.value for s in OPEN_FOLLOWUP_STATUSES)


class Priority(models.IntegerChoices):
    LOW = 1, "Low"
    NORMAL = 2, "Normal"
    HIGH = 3, "High"


class FollowUp(UUIDModel):
    report_request = models.OneToOneField(
        "reports.ReportRequest", on_delete=models.PROTECT, related_name="followup"
    )
    status = models.CharField(
        max_length=20, choices=FollowUpStatus.choices, default=FollowUpStatus.OPEN, db_index=True
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="followups"
    )
    priority = models.PositiveSmallIntegerField(choices=Priority.choices, default=Priority.NORMAL)
    next_reminder_at = models.DateTimeField(null=True, blank=True)
    escalation_level = models.PositiveSmallIntegerField(default=0)
    escalated_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-priority", "next_reminder_at"]
        indexes = [
            models.Index(fields=["status", "assigned_to"]),
            models.Index(fields=["status", "next_reminder_at"]),
        ]
        constraints = [
            # A closed follow-up has a close time; an open one does not.
            models.CheckConstraint(
                condition=(
                    Q(status__in=_OPEN_VALUES, closed_at__isnull=True)
                    | (~Q(status__in=_OPEN_VALUES) & Q(closed_at__isnull=False))
                ),
                name="followup_closed_at_matches_status",
            ),
        ]

    def __str__(self) -> str:
        return f"Follow-up {self.report_request_id} [{self.status}]"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_FOLLOWUP_STATUSES


class NoteChannel(models.TextChoices):
    CALL = "call", "Phone call"
    WHATSAPP = "whatsapp", "WhatsApp"
    OTHER = "other", "Other"


class NoteOutcome(models.TextChoices):
    NO_ANSWER = "no_answer", "No answer"
    CALLBACK = "callback", "Asked for callback"
    PROMISED_TO_PAY = "promised_to_pay", "Promised to pay"
    PAID_CLAIMED = "paid_claimed", "Says already paid"
    NOT_INTERESTED = "not_interested", "Not interested"
    WRONG_NUMBER = "wrong_number", "Wrong number"
    OTHER = "other", "Other"


class FollowUpNote(UUIDModel):
    """Immutable. Corrections are added as new notes so the history stays honest."""

    follow_up = models.ForeignKey(FollowUp, on_delete=models.PROTECT, related_name="notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    channel = models.CharField(max_length=16, choices=NoteChannel.choices, default=NoteChannel.CALL)
    outcome = models.CharField(max_length=20, choices=NoteOutcome.choices, blank=True)
    body = models.TextField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Note by {self.author_id} on {self.follow_up_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Follow-up notes cannot be edited; add a new note instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Follow-up notes cannot be deleted.")


class ReminderKind(models.TextChoices):
    AUTO = "auto", "Automatic"
    MANUAL = "manual", "Manual"


class ReminderStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    SENT = "sent", "Sent"
    SKIPPED = "skipped", "Skipped"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class Reminder(UUIDModel):
    follow_up = models.ForeignKey(FollowUp, on_delete=models.PROTECT, related_name="reminders")
    scheduled_for = models.DateTimeField()
    kind = models.CharField(max_length=8, choices=ReminderKind.choices, default=ReminderKind.AUTO)
    template_name = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=12, choices=ReminderStatus.choices, default=ReminderStatus.SCHEDULED)
    skip_reason = models.CharField(max_length=255, blank=True)
    sent_message = models.ForeignKey(
        "messaging.MessageLog", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    celery_task_id = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["scheduled_for"]
        indexes = [models.Index(fields=["status", "scheduled_for"])]

    def __str__(self) -> str:
        return f"Reminder {self.scheduled_for:%Y-%m-%d %H:%M} [{self.status}]"

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin

from .models import FollowUp, FollowUpNote, Reminder


@admin.register(FollowUp)
class FollowUpAdmin(ReadOnlyModelAdmin):
    list_display = (
        "report_request",
        "status",
        "assigned_to",
        "priority",
        "next_reminder_at",
        "escalation_level",
    )
    list_filter = ("status", "priority", "escalation_level")


@admin.register(FollowUpNote)
class FollowUpNoteAdmin(ReadOnlyModelAdmin):
    list_display = ("follow_up", "author", "channel", "outcome", "created_at")


@admin.register(Reminder)
class ReminderAdmin(ReadOnlyModelAdmin):
    list_display = ("follow_up", "scheduled_for", "kind", "status", "template_name")
    list_filter = ("status", "kind")

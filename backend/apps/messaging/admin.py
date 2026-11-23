from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin

from .models import MessageLog


@admin.register(MessageLog)
class MessageLogAdmin(ReadOnlyModelAdmin):
    list_display = ("created_at", "direction", "type", "template_name", "status", "error_code")
    list_filter = ("direction", "type", "status")
    search_fields = ("wa_message_id", "template_name")

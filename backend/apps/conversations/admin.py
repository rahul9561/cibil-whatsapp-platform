from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin

from .models import Conversation


@admin.register(Conversation)
class ConversationAdmin(ReadOnlyModelAdmin):
    list_display = ("customer", "state", "state_entered_at", "last_inbound_at")
    list_filter = ("state",)

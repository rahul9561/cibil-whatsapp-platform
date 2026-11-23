from django.contrib import admin

from .models import WebhookEvent


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """Django admin is an internal debugging view only. All changes go through the API (audited)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyModelAdmin):
    list_display = ("received_at", "source", "event_type", "event_id", "signature_valid", "processing_status")
    list_filter = ("source", "processing_status", "signature_valid")
    search_fields = ("event_id", "event_type")

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyModelAdmin):
    list_display = ("created_at", "actor_label", "actor_type", "action", "object_type", "object_id", "ip")
    list_filter = ("actor_type", "action")
    search_fields = ("action", "object_id", "actor_label", "request_id")

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin

from .models import ReportDocument, ReportRequest


@admin.register(ReportRequest)
class ReportRequestAdmin(ReadOnlyModelAdmin):
    list_display = ("public_ref", "report_status", "delivery_status", "price_paise", "created_at")
    list_filter = ("report_status", "delivery_status")
    search_fields = ("public_ref", "provider_session_ref")


@admin.register(ReportDocument)
class ReportDocumentAdmin(ReadOnlyModelAdmin):
    list_display = ("report_request", "size_bytes", "created_at", "purge_after", "purged_at")
    exclude = ("score",)

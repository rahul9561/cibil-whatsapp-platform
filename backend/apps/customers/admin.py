from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin

from .models import ConsentRecord, Customer


@admin.register(Customer)
class CustomerAdmin(ReadOnlyModelAdmin):
    # Encrypted fields are never rendered here; masked values only.
    list_display = ("id", "masked_phone", "masked_name", "masked_pan", "created_at", "opted_out_at")
    fields = (
        "id",
        "masked_phone",
        "masked_name",
        "masked_pan",
        "gender",
        "created_at",
        "opted_out_at",
        "anonymised_at",
    )
    readonly_fields = fields


@admin.register(ConsentRecord)
class ConsentRecordAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "customer", "purpose", "consent_text_version", "given_at", "withdrawn_at")

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin

from .models import Payment, PaymentLink, Refund


@admin.register(PaymentLink)
class PaymentLinkAdmin(ReadOnlyModelAdmin):
    list_display = ("reference_id", "status", "amount_paise", "sent_count", "expires_at", "paid_at")
    list_filter = ("status",)
    search_fields = ("reference_id", "razorpay_link_id")


@admin.register(Payment)
class PaymentAdmin(ReadOnlyModelAdmin):
    list_display = ("razorpay_payment_id", "status", "amount_paise", "amount_refunded_paise", "captured_at")
    list_filter = ("status", "method")
    search_fields = ("razorpay_payment_id", "razorpay_order_id")


@admin.register(Refund)
class RefundAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "payment", "amount_paise", "status", "requested_by", "approved_by", "created_at")
    list_filter = ("status",)

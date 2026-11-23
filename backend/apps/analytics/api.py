"""Dashboard summary, funnel and revenue (ARCHITECTURE §10, §11).

Days are bucketed in Python in the configured local timezone (IST): MySQL's CONVERT_TZ needs timezone
tables that a stock server does not have, so database-side truncation would silently return NULLs.
"""

import csv
import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import require
from apps.core.api import is_agent
from apps.followups.models import OPEN_FOLLOWUP_STATUSES, FollowUp, FollowUpStatus
from apps.payments.models import Payment, PaymentLink, PaymentLinkStatus, PaymentStatus, Refund, RefundStatus
from apps.reports.models import DeliveryStatus, ReportRequest, ReportStatus

MAX_RANGE_DAYS = 366


def _local_day_bounds(day: date) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), tz)
    return start, start + timedelta(days=1)


def _parse_range(request) -> tuple[date, date]:
    today = timezone.localdate()
    try:
        start = date.fromisoformat(request.query_params.get("from") or str(today - timedelta(days=29)))
        end = date.fromisoformat(request.query_params.get("to") or str(today))
    except ValueError as exc:
        raise ValidationError("Dates must be YYYY-MM-DD.") from exc
    if end < start or (end - start).days > MAX_RANGE_DAYS:
        raise ValidationError(f"'to' must be on/after 'from' and the range at most {MAX_RANGE_DAYS} days.")
    return start, end


def _paid_exists():
    return Exists(PaymentLink.objects.filter(report_request=OuterRef("pk"), status=PaymentLinkStatus.PAID))


class DashboardSummaryView(APIView):
    permission_classes = [require("dashboard.view")]

    @extend_schema(responses=dict)
    def get(self, request):
        start, end = _local_day_bounds(timezone.localdate())
        today_reqs = ReportRequest.objects.filter(created_at__gte=start, created_at__lt=end)
        payments_today = Payment.objects.filter(
            captured_at__gte=start,
            captured_at__lt=end,
            status__in=[PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED],
        )
        unpaid = ReportRequest.objects.annotate(_paid=_paid_exists()).filter(
            report_status=ReportStatus.GENERATED, _paid=False
        )
        followups = FollowUp.objects.filter(status__in=[s.value for s in OPEN_FOLLOWUP_STATUSES])
        if is_agent(request.user):
            followups = followups.filter(assigned_to=request.user)
        body = {
            "today": {
                **today_reqs.aggregate(
                    started=Count("id"),
                    generated=Count("id", filter=Q(generated_at__isnull=False)),
                    failed=Count(
                        "id", filter=Q(report_status__in=[ReportStatus.FAILED, ReportStatus.NO_HIT])
                    ),
                    delivered=Count(
                        "id",
                        filter=Q(
                            delivery_status__in=[
                                DeliveryStatus.SENT,
                                DeliveryStatus.DELIVERED,
                                DeliveryStatus.READ,
                            ]
                        ),
                    ),
                ),
                "payments": payments_today.count(),
            },
            "unpaid": unpaid.aggregate(count=Count("id"), value_paise=Sum("price_paise")),
            "followups": {
                "open": followups.count(),
                "overdue": followups.filter(next_reminder_at__lt=timezone.now()).count(),
                "escalated": followups.filter(status=FollowUpStatus.ESCALATED).count(),
                "unassigned": followups.filter(assigned_to__isnull=True).count(),
            },
        }
        if request.user.has_capability("analytics.revenue"):
            body["today"]["revenue_paise"] = payments_today.aggregate(s=Sum("amount_paise"))["s"] or 0
        body["unpaid"]["value_paise"] = body["unpaid"]["value_paise"] or 0
        return Response(body)


class FunnelView(APIView):
    permission_classes = [require("dashboard.view")]

    @extend_schema(parameters=[OpenApiParameter("from", str), OpenApiParameter("to", str)], responses=dict)
    def get(self, request):
        start, end = _parse_range(request)
        lo, _ = _local_day_bounds(start)
        _, hi = _local_day_bounds(end)
        qs = ReportRequest.objects.filter(created_at__gte=lo, created_at__lt=hi).annotate(
            _paid=_paid_exists()
        )
        stages = qs.aggregate(
            started=Count("id"),
            consented=Count("id", filter=Q(consent__isnull=False)),
            otp_verified=Count(
                "id",
                filter=Q(
                    report_status__in=[
                        ReportStatus.OTP_VERIFIED,
                        ReportStatus.FETCHING,
                        ReportStatus.GENERATED,
                        ReportStatus.NO_HIT,
                    ]
                )
                | Q(generated_at__isnull=False),
            ),
            generated=Count("id", filter=Q(generated_at__isnull=False)),
            paid=Count("id", filter=Q(_paid=True)),
            delivered=Count(
                "id",
                filter=Q(
                    delivery_status__in=[DeliveryStatus.SENT, DeliveryStatus.DELIVERED, DeliveryStatus.READ]
                ),
            ),
        )
        return Response({"from": start, "to": end, "stages": stages})


class RevenueRowSerializer(serializers.Serializer):
    period = serializers.CharField()
    payments = serializers.IntegerField()
    gross_paise = serializers.IntegerField()
    refunds_paise = serializers.IntegerField()
    net_paise = serializers.IntegerField()


class RevenueView(APIView):
    permission_classes = [require("analytics.revenue")]

    @extend_schema(
        parameters=[
            OpenApiParameter("from", str),
            OpenApiParameter("to", str),
            OpenApiParameter("granularity", str, enum=["day", "week", "month"]),
            OpenApiParameter("export", str, enum=["csv"]),
        ],
        responses=RevenueRowSerializer(many=True),
    )
    def get(self, request):
        start, end = _parse_range(request)
        granularity = request.query_params.get("granularity", "day")
        if granularity not in {"day", "week", "month"}:
            raise ValidationError("granularity must be day, week or month.")
        lo, _ = _local_day_bounds(start)
        _, hi = _local_day_bounds(end)

        def bucket(dt: datetime) -> str:
            d = timezone.localtime(dt).date()
            if granularity == "week":
                d = d - timedelta(days=d.weekday())
            elif granularity == "month":
                d = d.replace(day=1)
            return d.isoformat()

        rows = defaultdict(lambda: {"payments": 0, "gross_paise": 0, "refunds_paise": 0})
        for captured_at, amount in (
            Payment.objects.filter(captured_at__gte=lo, captured_at__lt=hi)
            .exclude(status=PaymentStatus.FAILED)
            .values_list("captured_at", "amount_paise")
            .iterator()
        ):
            r = rows[bucket(captured_at)]
            r["payments"] += 1
            r["gross_paise"] += amount
        for processed_at, amount in Refund.objects.filter(
            status=RefundStatus.PROCESSED, processed_at__gte=lo, processed_at__lt=hi
        ).values_list("processed_at", "amount_paise"):
            rows[bucket(processed_at)]["refunds_paise"] += amount
        data = [
            {"period": k, **v, "net_paise": v["gross_paise"] - v["refunds_paise"]}
            for k, v in sorted(rows.items())
        ]

        if request.query_params.get("export") == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf, fieldnames=["period", "payments", "gross_paise", "refunds_paise", "net_paise"]
            )
            writer.writeheader()
            writer.writerows(data)
            resp = HttpResponse(buf.getvalue(), content_type="text/csv")
            resp["Content-Disposition"] = f'attachment; filename="revenue-{start}-to-{end}.csv"'
            return resp
        return Response(data)


class SettingsView(APIView):
    """Read-only business configuration. Editing at runtime needs a DB-backed settings model (later phase)."""

    permission_classes = [require("settings.manage")]

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(
            {
                "report_price_paise": settings.REPORT_PRICE_PAISE,
                "report_validity_days": settings.REPORT_VALIDITY_DAYS,
                "report_pdf_retention_days": settings.REPORT_PDF_RETENTION_DAYS,
                "otp_max_attempts": settings.OTP_MAX_ATTEMPTS,
                "otp_max_resends": settings.OTP_MAX_RESENDS,
                "payment_link_expiry_hours": settings.PAYMENT_LINK_EXPIRY_HOURS,
                "followup_reminder_offsets_hours": settings.FOLLOWUP_REMINDER_OFFSETS_HOURS,
                "whatsapp_templates": settings.WHATSAPP_TEMPLATES,
                "adapters": {
                    "credit_provider": settings.CREDIT_PROVIDER,
                    "payment_gateway": settings.PAYMENT_GATEWAY,
                    "whatsapp": settings.WHATSAPP_GATEWAY,
                },
            }
        )

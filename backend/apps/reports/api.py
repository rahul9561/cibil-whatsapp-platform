"""Report request endpoints: list/detail, retry, cancel, audited PDF download, payment link resend."""

import django_filters
from django.db.models import Exists, OuterRef
from django.http import HttpResponse
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.audit import service as audit
from apps.core.api import CapabilityMixin, Conflict, agent_scope
from apps.payments.models import PaymentLink, PaymentLinkStatus
from apps.payments.services import PaymentService

from .models import DeliveryStatus, ReportRequest, ReportStatus
from .services import ReportService
from .storage import ReportStorage


class PaymentLinkBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentLink
        fields = [
            "id",
            "reference_id",
            "status",
            "amount_paise",
            "short_url",
            "expires_at",
            "paid_at",
            "sent_count",
            "last_sent_at",
        ]


class ReportRequestSerializer(serializers.ModelSerializer):
    customer_phone = serializers.CharField(source="customer.masked_phone")
    payment_status = serializers.SerializerMethodField()
    followup = serializers.SerializerMethodField()
    has_document = serializers.SerializerMethodField()

    class Meta:
        model = ReportRequest
        fields = [
            "id",
            "public_ref",
            "customer",
            "customer_phone",
            "report_status",
            "delivery_status",
            "payment_status",
            "price_paise",
            "currency",
            "failure_code",
            "failure_detail",
            "created_at",
            "status_changed_at",
            "generated_at",
            "expires_at",
            "delivered_at",
            "followup",
            "has_document",
        ]

    def get_payment_status(self, obj) -> str:
        statuses = {link.status for link in obj.payment_links.all()}
        if PaymentLinkStatus.PAID in statuses:
            return "paid"
        if statuses & {PaymentLinkStatus.ACTIVE, PaymentLinkStatus.PENDING}:
            return "awaiting_payment"
        return "no_link" if not statuses else "unpaid"

    def get_followup(self, obj) -> dict | None:
        f = getattr(obj, "followup", None)
        if f is None:
            return None
        return {
            "id": str(f.pk),
            "status": f.status,
            "assigned_to": f.assigned_to.email if f.assigned_to_id else None,
        }

    def get_has_document(self, obj) -> bool:
        doc = getattr(obj, "document", None)
        return bool(doc and not doc.is_purged)


class ReportRequestDetailSerializer(ReportRequestSerializer):
    payment_links = PaymentLinkBriefSerializer(many=True)

    class Meta(ReportRequestSerializer.Meta):
        fields = [
            *ReportRequestSerializer.Meta.fields,
            "payment_links",
            "otp_attempts",
            "otp_resends",
            "provider_name",
        ]


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class ReportRequestFilter(django_filters.FilterSet):
    report_status = django_filters.MultipleChoiceFilter(choices=ReportStatus.choices)
    delivery_status = django_filters.MultipleChoiceFilter(choices=DeliveryStatus.choices)
    unpaid = django_filters.BooleanFilter(method="filter_unpaid", label="Generated but not paid")
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")
    public_ref = django_filters.CharFilter(lookup_expr="iexact")

    class Meta:
        model = ReportRequest
        fields = ["customer"]

    def filter_unpaid(self, qs, name, value):
        paid = PaymentLink.objects.filter(report_request=OuterRef("pk"), status=PaymentLinkStatus.PAID)
        qs = qs.annotate(_paid=Exists(paid))
        if value:
            return qs.filter(report_status=ReportStatus.GENERATED, _paid=False)
        return qs.filter(_paid=True)


class ReportRequestViewSet(
    CapabilityMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    filterset_class = ReportRequestFilter
    ordering_fields = ["created_at", "status_changed_at", "price_paise"]
    ordering = ["-created_at"]
    capabilities = {
        "list": "report.view",
        "retrieve": "report.view",
        "retry": "report.manage",
        "cancel": "report.manage",
        "document": "report.download",
        "resend_payment_link": "payment.resend_link",
    }

    def get_queryset(self):
        qs = ReportRequest.objects.select_related(
            "customer", "followup__assigned_to", "document"
        ).prefetch_related("payment_links")
        return agent_scope(qs, self.request.user, "followup__assigned_to")

    def get_serializer_class(self):
        return ReportRequestDetailSerializer if self.action == "retrieve" else ReportRequestSerializer

    @extend_schema(request=None, responses=dict)
    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        """Re-run the stuck step: fetch (OTP verified / fetching) or delivery (paid, not delivered)."""
        from .tasks import deliver_report_task, fetch_report_task

        req = self.get_object()
        if req.report_status in {ReportStatus.OTP_VERIFIED, ReportStatus.FETCHING}:
            fetch_report_task.delay(str(req.pk))
            step = "fetch"
        elif (
            req.report_status == ReportStatus.GENERATED
            and req.delivery_status in {DeliveryStatus.NOT_DELIVERED, DeliveryStatus.FAILED}
            and req.payment_links.filter(status=PaymentLinkStatus.PAID).exists()
        ):
            ReportRequest.objects.filter(pk=req.pk).update(delivery_status=DeliveryStatus.NOT_DELIVERED)
            deliver_report_task.delay(str(req.pk))
            step = "delivery"
        else:
            raise Conflict("Nothing to retry in the current state.")
        audit.record("report.retry_requested", request=request, obj=req, changes={"step": step})
        return Response({"queued": step})

    @extend_schema(request=CancelSerializer, responses=ReportRequestSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        req = self.get_object()
        ser = CancelSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        req = ReportService().cancel(req.pk, by_user=request.user, reason=ser.validated_data["reason"])
        return Response(ReportRequestSerializer(self.get_queryset().get(pk=req.pk)).data)

    @extend_schema(responses={(200, "application/pdf"): OpenApiResponse(description="Report PDF")})
    @action(detail=True, methods=["get"])
    def document(self, request, pk=None):
        req = self.get_object()
        doc = getattr(req, "document", None)
        if doc is None or doc.is_purged:
            raise Conflict("No report document is available (not generated or purged).")
        content = ReportStorage().read(doc.storage_key, expected_sha256=doc.sha256)
        audit.record("report.document_downloaded", request=request, obj=req, changes={"size": doc.size_bytes})
        response = HttpResponse(content, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="credit-report-{req.public_ref}.pdf"'
        response["Cache-Control"] = "no-store, private"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    @extend_schema(request=None, responses=PaymentLinkBriefSerializer)
    @action(detail=True, methods=["post"], url_path="payment-links/resend")
    def resend_payment_link(self, request, pk=None):
        req = self.get_object()
        service = PaymentService()
        link = service.get_or_create_link(req.pk, created_by=request.user)
        service.send_link(link, sent_by=request.user)
        audit.record(
            "payment_link.resent", request=request, obj=link, changes={"sent_count": link.sent_count}
        )
        return Response(PaymentLinkBriefSerializer(link).data)

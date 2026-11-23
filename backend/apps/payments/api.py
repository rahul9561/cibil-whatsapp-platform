"""Payment links, payments and refunds (request -> four-eyes approval -> gateway)."""

import django_filters
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api import CapabilityMixin, agent_scope

from .models import Payment, PaymentLink, PaymentLinkStatus, PaymentStatus, Refund, RefundStatus
from .services import RefundService


class PaymentLinkSerializer(serializers.ModelSerializer):
    report_ref = serializers.CharField(source="report_request.public_ref")

    class Meta:
        model = PaymentLink
        fields = [
            "id",
            "report_request",
            "report_ref",
            "attempt",
            "reference_id",
            "razorpay_link_id",
            "short_url",
            "amount_paise",
            "currency",
            "status",
            "expires_at",
            "paid_at",
            "sent_count",
            "last_sent_at",
            "created_at",
        ]


class PaymentSerializer(serializers.ModelSerializer):
    report_ref = serializers.CharField(source="payment_link.report_request.public_ref")
    refundable_paise = serializers.IntegerField(read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id",
            "payment_link",
            "report_ref",
            "razorpay_payment_id",
            "amount_paise",
            "amount_refunded_paise",
            "refundable_paise",
            "currency",
            "method",
            "status",
            "captured_at",
            "created_at",
        ]


class RefundSerializer(serializers.ModelSerializer):
    requested_by = serializers.EmailField(source="requested_by.email", read_only=True)
    approved_by = serializers.EmailField(source="approved_by.email", read_only=True, default=None)
    razorpay_payment_id = serializers.CharField(source="payment.razorpay_payment_id", read_only=True)

    class Meta:
        model = Refund
        fields = [
            "id",
            "payment",
            "razorpay_payment_id",
            "razorpay_refund_id",
            "amount_paise",
            "reason",
            "status",
            "requested_by",
            "approved_by",
            "decided_at",
            "processed_at",
            "failure_reason",
            "created_at",
        ]


class RefundRequestSerializer(serializers.Serializer):
    amount_paise = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=2000)


class DecisionSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class DateRangeFilter(django_filters.FilterSet):
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")


class PaymentLinkFilter(DateRangeFilter):
    status = django_filters.MultipleChoiceFilter(choices=PaymentLinkStatus.choices)

    class Meta:
        model = PaymentLink
        fields = ["report_request"]


class PaymentFilter(DateRangeFilter):
    status = django_filters.MultipleChoiceFilter(choices=PaymentStatus.choices)

    class Meta:
        model = Payment
        fields = ["razorpay_payment_id"]


class RefundFilter(DateRangeFilter):
    status = django_filters.MultipleChoiceFilter(choices=RefundStatus.choices)

    class Meta:
        model = Refund
        fields = ["payment"]


class PaymentLinkViewSet(CapabilityMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = PaymentLinkSerializer
    filterset_class = PaymentLinkFilter
    ordering = ["-created_at"]
    capabilities = {"*": "payment.view"}

    def get_queryset(self):
        qs = PaymentLink.objects.select_related("report_request")
        return agent_scope(qs, self.request.user, "report_request__followup__assigned_to")


class PaymentViewSet(CapabilityMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = PaymentSerializer
    filterset_class = PaymentFilter
    ordering_fields = ["created_at", "amount_paise"]
    ordering = ["-created_at"]
    capabilities = {"list": "payment.view", "retrieve": "payment.view", "refunds": "refund.request"}

    def get_queryset(self):
        qs = Payment.objects.select_related("payment_link__report_request")
        return agent_scope(qs, self.request.user, "payment_link__report_request__followup__assigned_to")

    @extend_schema(request=RefundRequestSerializer, responses=RefundSerializer)
    @action(detail=True, methods=["post"])
    def refunds(self, request, pk=None):
        payment = self.get_object()
        ser = RefundRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        refund = RefundService().request(payment.pk, by_user=request.user, **ser.validated_data)
        return Response(RefundSerializer(refund).data, status=status.HTTP_201_CREATED)


class RefundViewSet(
    CapabilityMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = RefundSerializer
    filterset_class = RefundFilter
    ordering = ["-created_at"]
    queryset = Refund.objects.select_related("payment", "requested_by", "approved_by")
    capabilities = {
        "list": "payment.view",
        "retrieve": "payment.view",
        "approve": "refund.approve",
        "reject": "refund.approve",
    }

    @extend_schema(request=DecisionSerializer, responses=RefundSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from .tasks import process_refund_task

        ser = DecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        refund = RefundService().decide(
            self.get_object().pk, approve=True, by_user=request.user, note=ser.validated_data["note"]
        )
        process_refund_task.delay(str(refund.pk))
        refund.refresh_from_db()
        return Response(RefundSerializer(refund).data)

    @extend_schema(request=DecisionSerializer, responses=RefundSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        ser = DecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        refund = RefundService().decide(
            self.get_object().pk, approve=False, by_user=request.user, note=ser.validated_data["note"]
        )
        return Response(RefundSerializer(refund).data)

"""WhatsApp message log (read-only; sending goes through customers/{id}/messages)."""

import django_filters
from rest_framework import serializers, viewsets

from apps.core.api import CapabilityMixin, agent_scope

from .models import Direction, MessageLog, MessageStatus, MessageType


class MessageLogSerializer(serializers.ModelSerializer):
    sent_by = serializers.EmailField(source="sent_by.email", read_only=True, default=None)
    report_ref = serializers.CharField(
        source="related_report_request.public_ref", read_only=True, default=None
    )

    class Meta:
        model = MessageLog
        fields = [
            "id",
            "customer",
            "direction",
            "type",
            "template_name",
            "body_redacted",
            "status",
            "error_code",
            "error_detail",
            "sent_by",
            "report_ref",
            "wa_message_id",
            "created_at",
            "sent_at",
            "delivered_at",
            "read_at",
            "failed_at",
        ]


class MessageLogFilter(django_filters.FilterSet):
    direction = django_filters.ChoiceFilter(choices=Direction.choices)
    status = django_filters.MultipleChoiceFilter(choices=MessageStatus.choices)
    type = django_filters.MultipleChoiceFilter(choices=MessageType.choices)
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")

    class Meta:
        model = MessageLog
        fields = ["customer", "related_report_request", "template_name"]


class MessageLogViewSet(CapabilityMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = MessageLogSerializer
    filterset_class = MessageLogFilter
    ordering = ["-created_at"]
    capabilities = {"*": "customer.view"}

    def get_queryset(self):
        qs = MessageLog.objects.select_related("sent_by", "related_report_request")
        return agent_scope(qs, self.request.user, "customer__report_requests__followup__assigned_to")

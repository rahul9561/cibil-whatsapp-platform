import django_filters
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api import CapabilityMixin

from .models import ActorType, AuditLog
from .service import verify_chain


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            "id",
            "created_at",
            "actor",
            "actor_label",
            "actor_type",
            "action",
            "object_type",
            "object_id",
            "changes",
            "ip",
            "user_agent",
            "request_id",
        ]


class AuditLogFilter(django_filters.FilterSet):
    action = django_filters.CharFilter(lookup_expr="istartswith")
    actor_type = django_filters.ChoiceFilter(choices=ActorType.choices)
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lt")

    class Meta:
        model = AuditLog
        fields = ["actor", "object_type", "object_id", "request_id"]


class AuditLogViewSet(CapabilityMixin, viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer
    filterset_class = AuditLogFilter
    ordering = ["-id"]
    capabilities = {"*": "audit.view"}

    @extend_schema(responses=dict)
    @action(detail=False, methods=["get"])
    def verify(self, request):
        ok, bad_id, checked = verify_chain()
        return Response({"ok": ok, "first_bad_id": bad_id, "entries_checked": checked})

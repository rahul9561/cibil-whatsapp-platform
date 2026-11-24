"""Customer endpoints: masked by default, audited reveal, timeline, manual WhatsApp messages."""

import django_filters
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.audit import service as audit
from apps.core.api import CapabilityMixin, Conflict, agent_scope
from apps.core.masking import mask_email
from apps.followups.models import FollowUpNote
from apps.messaging.models import MessageLog
from apps.messaging.services import MessagingService
from apps.payments.services import PaymentService
from apps.reports.models import ReportRequest, ReportStatus

from .models import Customer

REVEALABLE = {
    "phone": "wa_id",
    "full_name": "full_name",
    "pan": "pan",
    "dob": "dob",
    "email": "email",
    "pincode": "pincode",
}


class CustomerSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(source="masked_phone")
    name = serializers.CharField(source="masked_name")
    pan = serializers.CharField(source="masked_pan")
    email = serializers.SerializerMethodField()
    open_request = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "id",
            "phone",
            "name",
            "pan",
            "email",
            "gender",
            "created_at",
            "opted_out_at",
            "open_request",
        ]

    def get_email(self, obj) -> str:
        return mask_email(obj.email)

    def get_open_request(self, obj) -> str | None:
        req = ReportRequest.objects.filter(active_lock=obj.pk).only("public_ref").first()
        return req.public_ref if req else None


class RevealSerializer(serializers.Serializer):
    fields = serializers.ListField(child=serializers.ChoiceField(choices=sorted(REVEALABLE)), min_length=1)
    reason = serializers.CharField(max_length=255)


class ManualMessageSerializer(serializers.Serializer):
    """Free text (inside the 24 h window) or a payment template.

    Template parameters are never taken from the client: the server builds them from the customer's open
    report (reference, price, payment link), so staff can't send a template with wrong or missing values.
    """

    text = serializers.CharField(max_length=4096, required=False)
    template_key = serializers.ChoiceField(choices=["payment_link", "payment_reminder"], required=False)

    def validate(self, data):
        if not data.get("text") and not data.get("template_key"):
            raise serializers.ValidationError("Provide text (inside the 24 h window) or a template.")
        if data.get("text") and data.get("template_key"):
            raise serializers.ValidationError("Send either text or a template, not both.")
        return data


class CustomerFilter(django_filters.FilterSet):
    phone = django_filters.CharFilter(method="by_phone", label="Exact phone number")
    pan = django_filters.CharFilter(method="by_pan", label="Exact PAN")
    opted_out = django_filters.BooleanFilter(field_name="opted_out_at", lookup_expr="isnull", exclude=True)
    created_after = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")

    class Meta:
        model = Customer
        fields: list[str] = []

    def by_phone(self, qs, name, value):
        return qs & Customer.objects.by_wa_id(value)

    def by_pan(self, qs, name, value):
        return qs & Customer.objects.by_pan(value)


class CustomerViewSet(
    CapabilityMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = CustomerSerializer
    filterset_class = CustomerFilter
    ordering_fields = ["created_at"]
    ordering = ["-created_at"]
    capabilities = {
        "list": "customer.view",
        "retrieve": "customer.view",
        "timeline": "customer.view",
        "reveal": "customer.reveal_pii",
        "messages": "message.send",
    }

    def get_queryset(self):
        return agent_scope(
            Customer.objects.all(), self.request.user, "report_requests__followup__assigned_to"
        )

    @extend_schema(request=RevealSerializer, responses=dict)
    @action(detail=True, methods=["post"])
    def reveal(self, request, pk=None):
        customer = self.get_object()
        ser = RevealSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        fields = sorted(set(ser.validated_data["fields"]))
        audit.record(
            "customer.pii_revealed",
            request=request,
            obj=customer,
            changes={"fields": fields, "reason": ser.validated_data["reason"]},
        )
        return Response({f: getattr(customer, REVEALABLE[f]) for f in fields})

    @extend_schema(responses=dict)
    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        customer = self.get_object()
        events = []
        for m in MessageLog.objects.filter(customer=customer).order_by("-created_at")[:100]:
            events.append(
                {
                    "at": m.created_at,
                    "kind": "message",
                    "direction": m.direction,
                    "type": m.type,
                    "status": m.status,
                    "text": m.body_redacted,
                    "template": m.template_name,
                }
            )
        for r in customer.report_requests.all():
            events.append(
                {
                    "at": r.created_at,
                    "kind": "report_request",
                    "ref": r.public_ref,
                    "report_status": r.report_status,
                    "delivery_status": r.delivery_status,
                }
            )
        for n in FollowUpNote.objects.filter(follow_up__report_request__customer=customer).select_related(
            "author"
        ):
            events.append(
                {
                    "at": n.created_at,
                    "kind": "note",
                    "author": n.author.email,
                    "channel": n.channel,
                    "outcome": n.outcome,
                    "text": n.body,
                }
            )
        events.sort(key=lambda e: e["at"], reverse=True)
        return Response(events[:200])

    @extend_schema(request=ManualMessageSerializer, responses=dict)
    @action(detail=True, methods=["post"])
    def messages(self, request, pk=None):
        customer = self.get_object()
        ser = ManualMessageSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        open_request = ReportRequest.objects.filter(active_lock=customer.pk).first()
        if data.get("template_key"):
            msg = self._send_payment_template(request, open_request, data["template_key"])
        else:
            msg = MessagingService().send(
                customer, text=data["text"], sent_by=request.user, related_request=open_request
            )
        audit.record(
            "message.sent_manually",
            request=request,
            obj=msg,
            changes={"type": msg.type, "template": msg.template_name},
        )
        return Response(
            {"id": str(msg.pk), "status": msg.status, "type": msg.type}, status=status.HTTP_201_CREATED
        )

    @staticmethod
    def _send_payment_template(request, open_request, template_key):
        """Reuse (or create) the payment link and send it exactly as the automatic flow does."""
        if open_request is None or open_request.report_status != ReportStatus.GENERATED:
            raise Conflict("This customer has no generated report awaiting payment.")
        service = PaymentService()
        link = service.get_or_create_link(open_request.pk, created_by=request.user)
        return service.send_link(link, sent_by=request.user, is_reminder=template_key == "payment_reminder")

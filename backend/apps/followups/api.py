"""Follow-up queue: assignment, status, notes, manual reminders (ARCHITECTURE §7)."""

import django_filters
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.accounts.models import Role
from apps.audit import service as audit
from apps.core.api import CapabilityMixin, Conflict, agent_scope

from .models import (
    OPEN_FOLLOWUP_STATUSES,
    FollowUp,
    FollowUpNote,
    FollowUpStatus,
    Priority,
    Reminder,
    ReminderKind,
    ReminderStatus,
)

STAFF_SETTABLE = {FollowUpStatus.IN_PROGRESS, FollowUpStatus.CLOSED_LOST, FollowUpStatus.CLOSED_MANUAL}
CLOSING = {FollowUpStatus.CLOSED_LOST, FollowUpStatus.CLOSED_MANUAL}


class NoteSerializer(serializers.ModelSerializer):
    author = serializers.EmailField(source="author.email", read_only=True)

    class Meta:
        model = FollowUpNote
        fields = ["id", "author", "channel", "outcome", "body", "created_at"]
        read_only_fields = ["id", "author", "created_at"]


class ReminderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reminder
        fields = [
            "id",
            "follow_up",
            "scheduled_for",
            "kind",
            "template_name",
            "status",
            "skip_reason",
            "created_at",
        ]
        read_only_fields = ["id", "follow_up", "kind", "template_name", "status", "skip_reason", "created_at"]

    def validate_scheduled_for(self, value):
        if value <= timezone.now():
            raise ValidationError("Must be in the future.")
        return value


class FollowUpSerializer(serializers.ModelSerializer):
    report_ref = serializers.CharField(source="report_request.public_ref", read_only=True)
    customer_phone = serializers.CharField(source="report_request.customer.masked_phone", read_only=True)
    amount_paise = serializers.IntegerField(source="report_request.price_paise", read_only=True)
    assigned_to_email = serializers.EmailField(source="assigned_to.email", read_only=True, default=None)
    last_note_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = FollowUp
        fields = [
            "id",
            "report_request",
            "report_ref",
            "customer_phone",
            "amount_paise",
            "status",
            "assigned_to",
            "assigned_to_email",
            "priority",
            "next_reminder_at",
            "escalation_level",
            "escalated_at",
            "closed_at",
            "close_reason",
            "last_note_at",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "report_request",
            "next_reminder_at",
            "escalation_level",
            "escalated_at",
            "closed_at",
            "created_at",
        ]
        extra_kwargs = {"assigned_to": {"queryset": get_user_model().objects.filter(is_active=True)}}

    def validate_status(self, value):
        if value not in STAFF_SETTABLE and value != FollowUpStatus.ESCALATED:
            raise ValidationError("This status is set automatically.")
        return value

    def validate(self, data):
        if data.get("status") in CLOSING and not (data.get("close_reason") or "").strip():
            raise ValidationError({"close_reason": "A reason is required to close a follow-up."})
        return data


class FollowUpFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=FollowUpStatus.choices)
    open = django_filters.BooleanFilter(method="filter_open")
    assignee = django_filters.CharFilter(method="filter_assignee", label='User id, "me" or "unassigned"')
    overdue = django_filters.BooleanFilter(method="filter_overdue")
    priority = django_filters.MultipleChoiceFilter(choices=Priority.choices)

    class Meta:
        model = FollowUp
        fields: list[str] = []

    def filter_open(self, qs, name, value):
        open_values = [s.value for s in OPEN_FOLLOWUP_STATUSES]
        return qs.filter(status__in=open_values) if value else qs.exclude(status__in=open_values)

    def filter_assignee(self, qs, name, value):
        if value == "me":
            return qs.filter(assigned_to=self.request.user)
        if value == "unassigned":
            return qs.filter(assigned_to__isnull=True)
        return qs.filter(assigned_to_id=value)

    def filter_overdue(self, qs, name, value):
        cond = {
            "status__in": [s.value for s in OPEN_FOLLOWUP_STATUSES],
            "next_reminder_at__lt": timezone.now(),
        }
        return qs.filter(**cond) if value else qs.exclude(**cond)


class FollowUpViewSet(
    CapabilityMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = FollowUpSerializer
    filterset_class = FollowUpFilter
    ordering_fields = ["created_at", "next_reminder_at", "priority", "escalation_level"]
    ordering = ["-priority", "next_reminder_at"]
    http_method_names = ["get", "patch", "post", "head", "options"]
    capabilities = {
        "list": "followup.view",
        "retrieve": "followup.view",
        "partial_update": "followup.manage",
        "notes": "followup.view",
        "reminders": "followup.view",
    }

    def get_queryset(self):
        qs = FollowUp.objects.select_related("report_request__customer", "assigned_to").annotate(
            last_note_at=Max("notes__created_at")
        )
        return agent_scope(qs, self.request.user, "assigned_to")

    def perform_update(self, serializer):
        followup = serializer.instance
        data = serializer.validated_data
        user = self.request.user
        if (
            {"assigned_to", "priority"} & data.keys() or data.get("status") == FollowUpStatus.ESCALATED
        ) and not user.has_capability("followup.assign"):
            raise PermissionDenied("Only admins can assign, prioritise or escalate follow-ups.")
        if "assigned_to" in data and data["assigned_to"] and data["assigned_to"].role == Role.AUDITOR:
            raise ValidationError({"assigned_to": "Auditors cannot be assigned follow-ups."})
        if not followup.is_open:
            raise Conflict("This follow-up is closed.")
        before = {k: str(getattr(followup, k + ("_id" if k == "assigned_to" else ""))) for k in data}
        with transaction.atomic():
            extra = {}
            if data.get("status") in CLOSING:
                extra = {"closed_at": timezone.now(), "next_reminder_at": None}
            if data.get("status") == FollowUpStatus.ESCALATED:
                extra = {"escalated_at": timezone.now(), "escalation_level": followup.escalation_level + 1}
            instance = serializer.save(**extra)
            if data.get("status") in CLOSING:
                instance.reminders.filter(status=ReminderStatus.SCHEDULED).update(
                    status=ReminderStatus.CANCELLED, skip_reason="followup_closed"
                )
            audit.record(
                "followup.updated",
                request=self.request,
                obj=instance,
                changes={
                    "before": before,
                    "after": {k: str(v.pk if hasattr(v, "pk") else v) for k, v in data.items()},
                },
            )

    @extend_schema(request=NoteSerializer, responses=NoteSerializer(many=True))
    @action(detail=True, methods=["get", "post"])
    def notes(self, request, pk=None):
        followup = self.get_object()
        if request.method == "GET":
            return Response(NoteSerializer(followup.notes.select_related("author"), many=True).data)
        if not request.user.has_capability("followup.manage"):
            raise PermissionDenied()
        ser = NoteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        note = ser.save(follow_up=followup, author=request.user)
        if followup.status == FollowUpStatus.OPEN:
            followup.status = FollowUpStatus.IN_PROGRESS
            followup.save(update_fields=["status", "updated_at"])
        audit.record(
            "followup.note_added",
            request=request,
            obj=followup,
            changes={"note": str(note.pk), "outcome": note.outcome},
        )
        return Response(NoteSerializer(note).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ReminderSerializer, responses=ReminderSerializer(many=True))
    @action(detail=True, methods=["get", "post"])
    def reminders(self, request, pk=None):
        followup = self.get_object()
        if request.method == "GET":
            return Response(ReminderSerializer(followup.reminders.all(), many=True).data)
        if not request.user.has_capability("followup.manage"):
            raise PermissionDenied()
        if not followup.is_open:
            raise Conflict("This follow-up is closed.")
        ser = ReminderSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        from django.conf import settings

        reminder = ser.save(
            follow_up=followup,
            kind=ReminderKind.MANUAL,
            created_by=request.user,
            template_name=settings.WHATSAPP_TEMPLATES["payment_reminder"],
        )
        next_at = (
            followup.reminders.filter(status=ReminderStatus.SCHEDULED)
            .order_by("scheduled_for")
            .values_list("scheduled_for", flat=True)
            .first()
        )
        FollowUp.objects.filter(pk=followup.pk).update(next_reminder_at=next_at)
        audit.record(
            "followup.reminder_scheduled",
            request=request,
            obj=followup,
            changes={"reminder": str(reminder.pk), "at": reminder.scheduled_for.isoformat()},
        )
        return Response(ReminderSerializer(reminder).data, status=status.HTTP_201_CREATED)


class ReminderViewSet(CapabilityMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """DELETE cancels a scheduled reminder (it is kept for history, never removed)."""

    serializer_class = ReminderSerializer
    capabilities = {"destroy": "followup.manage"}

    def get_queryset(self):
        return agent_scope(
            Reminder.objects.select_related("follow_up"), self.request.user, "follow_up__assigned_to"
        )

    def perform_destroy(self, instance):
        if instance.status != ReminderStatus.SCHEDULED:
            raise Conflict("Only scheduled reminders can be cancelled.")
        instance.status, instance.skip_reason = ReminderStatus.CANCELLED, "cancelled_by_staff"
        instance.save(update_fields=["status", "skip_reason", "updated_at"])
        next_at = (
            instance.follow_up.reminders.filter(status=ReminderStatus.SCHEDULED)
            .order_by("scheduled_for")
            .values_list("scheduled_for", flat=True)
            .first()
        )
        FollowUp.objects.filter(pk=instance.follow_up_id).update(next_reminder_at=next_at)
        audit.record(
            "followup.reminder_cancelled",
            request=self.request,
            obj=instance.follow_up,
            changes={"reminder": str(instance.pk)},
        )

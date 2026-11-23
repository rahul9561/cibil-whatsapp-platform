"""Staff management (Super Admin only): create, update role/status, reset 2FA. Users are never deleted."""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.audit import service as audit
from apps.core.api import CapabilityMixin

from .models import Role

User = get_user_model()


def end_sessions(user) -> int:
    """Log the user out everywhere (used on deactivation, role change and 2FA reset)."""
    ended = 0
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        if session.get_decoded().get("_auth_user_id") == str(user.pk):
            session.delete()
            ended += 1
    return ended


class StaffSerializer(serializers.ModelSerializer):
    two_factor_enrolled = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "is_active",
            "last_login",
            "last_login_ip",
            "date_joined",
            "two_factor_enrolled",
        ]
        read_only_fields = ["id", "email", "last_login", "last_login_ip", "date_joined"]

    def get_two_factor_enrolled(self, obj) -> bool:
        return TOTPDevice.objects.filter(user=obj, confirmed=True).exists()


class StaffCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "role", "password"]

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email=value).exists():
            raise ValidationError("A user with this email already exists.")
        return value

    def validate(self, data):
        validate_password(data["password"], User(email=data["email"], first_name=data.get("first_name", "")))
        return data


class StaffViewSet(
    CapabilityMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = User.objects.order_by("email")
    filterset_fields = ["role", "is_active"]
    http_method_names = ["get", "post", "patch", "head", "options"]
    capabilities = {"*": "staff.manage"}

    def get_serializer_class(self):
        return StaffCreateSerializer if self.action == "create" else StaffSerializer

    @extend_schema(request=StaffCreateSerializer, responses=StaffSerializer)
    def create(self, request, *args, **kwargs):
        ser = StaffCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        user = User.objects.create_user(
            email=data["email"],
            password=data["password"],
            role=data["role"],
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
        )
        audit.record("staff.created", request=request, obj=user, changes={"role": user.role})
        return Response(StaffSerializer(user).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        user, data, me = serializer.instance, serializer.validated_data, self.request.user
        if user.pk == me.pk and (
            "role" in data and data["role"] != me.role or data.get("is_active") is False
        ):
            raise ValidationError("You cannot change your own role or deactivate yourself.")
        losing_super = user.role == Role.SUPER_ADMIN and (
            data.get("role", user.role) != Role.SUPER_ADMIN or data.get("is_active") is False
        )
        with transaction.atomic():
            # Backstop: via the API the editor is itself an active super admin, but keep the invariant
            # explicit in case other code paths (commands, future roles) ever edit staff.
            if (
                losing_super
                and not User.objects.select_for_update()
                .filter(role=Role.SUPER_ADMIN, is_active=True)
                .exclude(pk=user.pk)
                .exists()
            ):
                raise ValidationError("At least one active Super Admin must remain.")
            before = {k: str(getattr(user, k)) for k in data}
            user = serializer.save()
            if user.role == Role.SUPER_ADMIN:
                User.objects.filter(pk=user.pk).update(is_staff=True, is_superuser=True)
            else:
                User.objects.filter(pk=user.pk).update(is_staff=False, is_superuser=False)
            ended = end_sessions(user) if ("role" in data or data.get("is_active") is False) else 0
            audit.record(
                "staff.updated",
                request=self.request,
                obj=user,
                changes={
                    "before": before,
                    "after": {k: str(v) for k, v in data.items()},
                    "sessions_ended": ended,
                },
            )

    @extend_schema(request=None, responses=StaffSerializer)
    @action(detail=True, methods=["post"], url_path="reset-2fa")
    def reset_2fa(self, request, pk=None):
        user = self.get_object()
        removed, _ = TOTPDevice.objects.filter(user=user).delete()
        ended = end_sessions(user)
        audit.record(
            "staff.2fa_reset",
            request=request,
            obj=user,
            changes={"devices_removed": removed, "sessions_ended": ended},
        )
        return Response(StaffSerializer(user).data)

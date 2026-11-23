"""Session authentication API for the admin SPA (ARCHITECTURE §6.6).

Flow: GET csrf → POST login → (2FA) POST 2fa/setup + 2fa/confirm on first use, or POST 2fa/verify.
Until 2FA is satisfied the session can only reach these endpoints; everything else is denied by
the default `IsFullyAuthenticated` permission.
"""

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django_otp import devices_for_user, user_has_device
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.crypto import normalize_email
from apps.core.http import client_ip

from . import lockout
from .capabilities import capabilities_for
from .permissions import IsPasswordAuthenticated, is_verified, second_factor_satisfied

GENERIC_LOGIN_ERROR = "Invalid email or password."


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False)


class TokenSerializer(serializers.Serializer):
    token = serializers.RegexField(r"^\d{6}$", error_messages={"invalid": "Enter the 6-digit code."})


def session_state(request) -> dict:
    user = request.user
    enrolled = user_has_device(user, confirmed=True)
    return {
        "user": {
            "id": str(user.pk),
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role,
            "capabilities": capabilities_for(user.role),
        },
        "two_factor": {
            "required": user.requires_2fa,
            "enrolled": enrolled,
            "verified": is_verified(user),
        },
        "fully_authenticated": second_factor_satisfied(request),
    }


class CsrfView(APIView):
    permission_classes = [AllowAny]

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return Response({"detail": "ok"})


@method_decorator(csrf_protect, name="dispatch")  # DRF skips CSRF for anonymous requests; login needs it.
class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        ser = LoginSerializer(data=request.data)
        if not ser.is_valid():
            return Response({"detail": GENERIC_LOGIN_ERROR}, status=status.HTTP_400_BAD_REQUEST)
        email = normalize_email(ser.validated_data["email"])
        known = get_user_model().objects.filter(email=email).values_list("pk", flat=True).first()

        if lockout.is_locked(email):
            audit.record(
                "auth.login_locked", request=request, object_type="accounts.user", object_id=str(known or "")
            )
            return Response(
                {"detail": "Too many failed attempts. Try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        user = authenticate(request, email=email, password=ser.validated_data["password"])
        if user is None:
            attempts = lockout.register_failure(email)
            audit.record(
                "auth.login_failed",
                request=request,
                object_type="accounts.user",
                object_id=str(known or ""),
                changes={"attempts": attempts, "known_user": bool(known)},
            )
            return Response({"detail": GENERIC_LOGIN_ERROR}, status=status.HTTP_400_BAD_REQUEST)

        lockout.clear(email)
        login(request, user)  # rotates the session key
        user.last_login_ip = client_ip(request)
        user.save(update_fields=["last_login_ip"])
        audit.record("auth.login", request=request, obj=user)
        return Response(session_state(request))


class MeView(APIView):
    permission_classes = [IsPasswordAuthenticated]

    def get(self, request):
        return Response(session_state(request))


class LogoutView(APIView):
    permission_classes = [IsPasswordAuthenticated]

    def post(self, request):
        audit.record("auth.logout", request=request, obj=request.user)
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TwoFactorSetupView(APIView):
    """Start TOTP enrolment. Only possible while the user has no confirmed device;
    resetting an existing device is an admin action (Phase 5)."""

    permission_classes = [IsPasswordAuthenticated]

    def post(self, request):
        user = request.user
        if user_has_device(user, confirmed=True):
            return Response(
                {"detail": "Two-factor authentication is already set up."}, status=status.HTTP_409_CONFLICT
            )
        TOTPDevice.objects.filter(user=user, confirmed=False).delete()
        device = TOTPDevice.objects.create(user=user, name="Authenticator app", confirmed=False)
        audit.record("auth.2fa_setup_started", request=request, obj=user)
        # The SPA renders this as a QR code. It contains the TOTP secret: never log it.
        return Response({"otpauth_url": device.config_url})


class TwoFactorConfirmView(APIView):
    permission_classes = [IsPasswordAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"

    def post(self, request):
        ser = TokenSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        device = TOTPDevice.objects.filter(user=request.user, confirmed=False).first()
        if device is None:
            return Response({"detail": "Start setup first."}, status=status.HTTP_400_BAD_REQUEST)
        if not device.verify_token(ser.validated_data["token"]):
            audit.record("auth.2fa_setup_failed", request=request, obj=request.user)
            return Response({"detail": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST)
        device.confirmed = True
        device.save(update_fields=["confirmed"])
        otp_login(request, device)
        request.session.cycle_key()
        audit.record("auth.2fa_enrolled", request=request, obj=request.user)
        return Response(session_state(request))


class TwoFactorVerifyView(APIView):
    permission_classes = [IsPasswordAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"

    def post(self, request):
        ser = TokenSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        token = ser.validated_data["token"]
        for device in devices_for_user(request.user, confirmed=True):
            allowed, _ = device.verify_is_allowed()  # django-otp's own per-device backoff
            if allowed and device.verify_token(token):
                otp_login(request, device)
                request.session.cycle_key()
                audit.record("auth.2fa_verified", request=request, obj=request.user)
                return Response(session_state(request))
        audit.record("auth.2fa_failed", request=request, obj=request.user)
        return Response({"detail": "Invalid code."}, status=status.HTTP_400_BAD_REQUEST)

from django_otp import user_has_device
from rest_framework.permissions import BasePermission


def is_verified(user) -> bool:
    """django-otp's OTPMiddleware adds `is_verified()`; users fresh from authenticate() lack it."""
    check = getattr(user, "is_verified", None)
    return bool(check and check())


def second_factor_satisfied(request) -> bool:
    """True if the session has passed 2FA, or 2FA is neither required nor set up for this user."""
    user = request.user
    if is_verified(user):
        return True
    return not user.requires_2fa and not user_has_device(user, confirmed=True)


class IsFullyAuthenticated(BasePermission):
    """Default for every API view: logged in, active, and past 2FA when it applies."""

    message = "Authentication and two-factor verification required."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and second_factor_satisfied(request))


class IsPasswordAuthenticated(BasePermission):
    """Password step done; 2FA may still be pending. Only for the 2FA setup/verify endpoints."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_active)


def require(capability: str) -> type[BasePermission]:
    """`permission_classes = [require("refund.approve")]` — implies IsFullyAuthenticated."""
    from .capabilities import CAPABILITIES

    if capability not in CAPABILITIES:
        raise ValueError(f"Unknown capability: {capability}")

    class _HasCapability(IsFullyAuthenticated):
        message = "You do not have permission to perform this action."

        def has_permission(self, request, view):
            return super().has_permission(request, view) and request.user.has_capability(capability)

    _HasCapability.__name__ = f"Require[{capability}]"
    return _HasCapability

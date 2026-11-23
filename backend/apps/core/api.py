"""Shared pieces for the admin REST API (ARCHITECTURE §8)."""

from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.accounts.models import Role
from apps.accounts.permissions import require

from .integrations import IntegrationError, LockNotAcquired


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state."
    default_code = "conflict"


def exception_handler(exc, context):
    """Map domain errors to clean API responses; never leak internals."""
    from apps.reports.models import InvalidTransition

    if isinstance(exc, InvalidTransition):
        return Response({"detail": str(exc), "code": "invalid_transition"}, status=status.HTTP_409_CONFLICT)
    if isinstance(exc, LockNotAcquired):
        return Response(
            {"detail": "Another operation is in progress. Try again.", "code": "busy"},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, IntegrationError):
        return Response(
            {"detail": "The external service could not complete this request.", "code": exc.code},
            status=status.HTTP_502_BAD_GATEWAY if exc.code.endswith("timeout") else 422,
        )
    if isinstance(exc, ObjectDoesNotExist):
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    return drf_exception_handler(exc, context)


class CapabilityMixin:
    """Per-action capability, e.g. `capabilities = {"list": "report.view", "*": "report.manage"}`."""

    capabilities: dict[str, str] = {}

    def get_permissions(self):
        cap = self.capabilities.get(getattr(self, "action", None) or "") or self.capabilities.get("*")
        if cap is None:
            raise RuntimeError(f"{type(self).__name__}: no capability for action {self.action!r}")
        return [require(cap)()]


def is_agent(user) -> bool:
    return getattr(user, "role", None) == Role.AGENT  # anonymous during schema generation


def agent_scope(queryset, user, assignee_path: str):
    """Agents only see records whose follow-up is assigned to them ("assigned only", ARCHITECTURE §6.6)."""
    if is_agent(user):
        return queryset.filter(**{assignee_path: user}).distinct()
    return queryset

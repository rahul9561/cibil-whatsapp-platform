"""Test-only URLs: one view behind the default permission, one behind a capability."""

from django.urls import include, path
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import require


class Protected(APIView):
    def get(self, request):
        return Response({"ok": True})


class StaffOnly(APIView):
    permission_classes = [require("staff.manage")]

    def get(self, request):
        return Response({"ok": True})


urlpatterns = [
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("protected", Protected.as_view()),
    path("staff-only", StaffOnly.as_view()),
]

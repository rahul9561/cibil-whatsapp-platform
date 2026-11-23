"""Admin API v1 (ARCHITECTURE §8). Every view is capability-checked; see apps/accounts/capabilities.py."""

from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from apps.accounts.staff_api import StaffViewSet
from apps.analytics.api import DashboardSummaryView, FunnelView, RevenueView, SettingsView
from apps.audit.api import AuditLogViewSet
from apps.customers.api import CustomerViewSet
from apps.followups.api import FollowUpViewSet, ReminderViewSet
from apps.messaging.api import MessageLogViewSet
from apps.payments.api import PaymentLinkViewSet, PaymentViewSet, RefundViewSet
from apps.reports.api import ReportRequestViewSet

router = DefaultRouter(trailing_slash=False)
router.include_root_view = False
router.register("customers", CustomerViewSet, basename="customer")
router.register("report-requests", ReportRequestViewSet, basename="report-request")
router.register("payment-links", PaymentLinkViewSet, basename="payment-link")
router.register("payments", PaymentViewSet, basename="payment")
router.register("refunds", RefundViewSet, basename="refund")
router.register("follow-ups", FollowUpViewSet, basename="follow-up")
router.register("reminders", ReminderViewSet, basename="reminder")
router.register("messages", MessageLogViewSet, basename="message")
router.register("users", StaffViewSet, basename="user")
router.register("audit-logs", AuditLogViewSet, basename="audit-log")

urlpatterns = [
    path("auth/", include("apps.accounts.urls")),
    path("dashboard/summary", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("analytics/funnel", FunnelView.as_view(), name="analytics-funnel"),
    path("analytics/revenue", RevenueView.as_view(), name="analytics-revenue"),
    path("settings", SettingsView.as_view(), name="settings"),
    path("schema", SpectacularAPIView.as_view(), name="schema"),
    path("docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("", include(router.urls)),
]

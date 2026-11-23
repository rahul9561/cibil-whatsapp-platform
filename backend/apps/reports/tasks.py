"""Report pipeline tasks. Arguments are ids only: never PII, never an OTP (ARCHITECTURE §6.5).

OTP verification is deliberately not a task: the OTP would sit in the broker. Phase 7 verifies it from the
webhook-processing path while it is held in memory.
"""

from celery import shared_task

from apps.core.integrations import PermanentError
from apps.core.tasks import run_with_retries

from .models import ReportStatus
from .services import OtpResendLimit, ReportService


@shared_task(bind=True, max_retries=4, name="reports.initiate_otp")
def initiate_otp_task(self, request_id: str):
    service = ReportService()
    try:
        return run_with_retries(
            self,
            lambda: service.initiate_otp(request_id) and "otp_sent",
            on_give_up=lambda exc: service.mark_failed(request_id, "provider_unavailable"),
        )
    except OtpResendLimit:
        raise
    except PermanentError as exc:  # e.g. provider rejects the applicant: a business outcome, not a crash
        return service.mark_failed(request_id, exc.code)


@shared_task(bind=True, max_retries=5, name="reports.fetch_report")
def fetch_report_task(self, request_id: str):
    service = ReportService()
    status = run_with_retries(
        self,
        lambda: service.fetch_report(request_id),
        on_give_up=lambda exc: service.mark_failed(request_id, "provider_unavailable"),
    )
    if status == ReportStatus.GENERATED:
        from apps.payments.tasks import create_payment_link_task

        create_payment_link_task.delay(str(request_id))
    return status


@shared_task(bind=True, max_retries=6, name="reports.deliver_report")
def deliver_report_task(self, request_id: str):
    service = ReportService()
    msg = run_with_retries(self, lambda: service.deliver(request_id))
    return str(msg.pk) if msg else None

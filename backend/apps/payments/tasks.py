from celery import shared_task

from apps.core.tasks import run_with_retries
from apps.followups.services import FollowUpService
from apps.reports.models import ReportRequest

from .models import PaymentLinkStatus
from .services import PaymentService


@shared_task(bind=True, max_retries=5, name="payments.create_payment_link")
def create_payment_link_task(self, request_id: str):
    """GENERATED report -> payment link -> follow-up opened -> link sent on WhatsApp."""

    def run():
        service = PaymentService()
        link = service.get_or_create_link(request_id)
        FollowUpService().open_for(link.report_request)
        if link.sent_count == 0:
            service.send_link(link)
        return str(link.pk)

    return run_with_retries(self, run)


@shared_task(bind=True, max_retries=8, name="payments.confirm_payment")
def confirm_payment_task(self, razorpay_link_id: str):
    """Called from the webhook processor (Phase 6) and the reconciliation job (Phase 8)."""

    def run():
        link = PaymentService().confirm_link_payment(razorpay_link_id)
        if link.status != PaymentLinkStatus.PAID:
            return "not_paid"
        req = ReportRequest.objects.get(pk=link.report_request_id)
        FollowUpService().close_paid(req)
        from apps.reports.tasks import deliver_report_task

        deliver_report_task.delay(str(req.pk))
        return "paid"

    return run_with_retries(self, run)


@shared_task(bind=True, max_retries=6, name="payments.process_refund")
def process_refund_task(self, refund_id: str):
    from .services import RefundService

    refund = run_with_retries(self, lambda: RefundService().process(refund_id))
    return refund.status if refund else None

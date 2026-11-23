"""Tell the customer about background outcomes and move the conversation along.

State updates are conditional (only from the state we expect), so they never clobber a newer state
set by the bot while the customer was typing.
"""

import logging

from django.utils import timezone

from apps.core.integrations import IntegrationError
from apps.messaging.services import MessagingService
from apps.reports.models import ReportRequest, ReportStatus

from . import copy as t
from .models import Conversation
from .models import ConversationState as S

logger = logging.getLogger(__name__)


def _move(req: ReportRequest, from_states: set[str], to_state: str) -> None:
    Conversation.objects.filter(customer_id=req.customer_id, state__in=from_states).update(
        state=to_state, state_entered_at=timezone.now()
    )


def _tell(req: ReportRequest, text: str, key: str) -> None:
    try:
        MessagingService().send(
            req.customer, text=text, idempotency_key=f"notify:{req.pk}:{key}", related_request=req
        )
    except IntegrationError as exc:  # e.g. window closed: the customer sees the outcome next time they write
        logger.warning("could not notify customer about %s: %s", key, exc.code)


def report_outcome(request_id) -> None:
    req = ReportRequest.objects.select_related("customer").get(pk=request_id)
    waiting = {S.FETCHING_REPORT, S.AWAITING_OTP, S.OTP_FAILED}
    if req.report_status == ReportStatus.GENERATED:
        _move(req, waiting, S.AWAITING_PAYMENT)  # the payment link itself is sent by the payments task
    elif req.report_status == ReportStatus.NO_HIT:
        _move(req, waiting, S.NO_HIT)
        _tell(req, t.NO_HIT, "no_hit")
    elif req.report_status == ReportStatus.FAILED:
        _move(req, waiting, S.REPORT_FAILED)
        _tell(req, t.REJECTED if req.failure_code == "applicant_rejected" else t.REPORT_FAILED, "failed")


def report_delivered(request_id) -> None:
    req = ReportRequest.objects.get(pk=request_id)
    _move(req, {S.AWAITING_PAYMENT, S.FETCHING_REPORT}, S.DELIVERED)

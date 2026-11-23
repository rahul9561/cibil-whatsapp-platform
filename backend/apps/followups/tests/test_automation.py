"""Scheduled jobs under frozen time: reminder cadence, stop conditions, escalation, expiry, purge."""

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone
from freezegun import freeze_time

from apps.accounts.models import Role
from apps.conversations.models import Conversation
from apps.conversations.models import ConversationState as S
from apps.core.testing.api import client_for, make_request
from apps.core.testing.factories import CONSENT_TEXT, CustomerFactory, UserFactory
from apps.followups.models import FollowUp, FollowUpNote, FollowUpStatus, Priority, Reminder, ReminderStatus
from apps.followups.tasks import dispatch_due_reminders, escalate_overdue_followups, send_reminder
from apps.messaging.gateway.mock import outbox
from apps.payments.gateway.mock import simulate_payment
from apps.payments.models import PaymentLinkStatus
from apps.payments.services import PaymentService
from apps.reports.maintenance import expire_stale_requests, purge_expired_documents
from apps.reports.models import ReportStatus
from apps.reports.services import ReportService

pytestmark = pytest.mark.django_db
T0 = "2026-11-23 10:00:00+05:30"


def statuses(req):
    return list(
        Reminder.objects.filter(follow_up__report_request=req)
        .order_by("scheduled_for")
        .values_list("status", flat=True)
    )


# --- reminders -------------------------------------------------------------------------------------------


def test_reminder_cadence_window_and_link_replacement():
    with freeze_time(T0) as clock:
        req = make_request()
        first_link = req.payment_links.get()
        assert dispatch_due_reminders() == 0

        clock.tick(timedelta(minutes=61))  # +1h: inside the 24 h window -> free text
        assert dispatch_due_reminders() == 1
        assert outbox()[-1]["kind"] == "text" and first_link.short_url in outbox()[-1]["body"]
        assert statuses(req) == ["sent", "scheduled", "scheduled"]
        fu = FollowUp.objects.get(report_request=req)
        assert fu.next_reminder_at == fu.reminders.order_by("scheduled_for")[1].scheduled_for

        clock.tick(timedelta(hours=24))  # +25h: window closed -> approved template
        assert dispatch_due_reminders() == 1
        assert outbox()[-1]["kind"] == "template"
        assert outbox()[-1]["template"] == settings.WHATSAPP_TEMPLATES["payment_reminder"]

        clock.tick(timedelta(hours=48))  # +73h: the original 72 h link has expired -> a fresh link is sent
        assert dispatch_due_reminders() == 1
        new_link = req.payment_links.get(status=PaymentLinkStatus.ACTIVE)
        assert new_link.attempt == 2 and new_link.short_url in outbox()[-1]["params"]
        assert statuses(req) == ["sent", "sent", "sent"]
        assert FollowUp.objects.get(pk=fu.pk).next_reminder_at is None
        assert dispatch_due_reminders() == 0


def test_a_reminder_is_sent_once():
    with freeze_time(T0) as clock:
        req = make_request()
        clock.tick(timedelta(minutes=61))
        sent_before = len(outbox())
        assert dispatch_due_reminders() == 1
        assert dispatch_due_reminders() == 0
        reminder = Reminder.objects.get(follow_up__report_request=req, status=ReminderStatus.SENT)
        assert send_reminder(reminder.pk) == "not_queued"
        assert len(outbox()) == sent_before + 1


def test_stuck_queued_reminder_is_picked_up_again():
    with freeze_time(T0) as clock:
        req = make_request()
        clock.tick(timedelta(minutes=61))
        r = Reminder.objects.filter(follow_up__report_request=req).order_by("scheduled_for").first()
        # Claimed just now (as the dispatcher does, which stamps updated_at), then the worker died.
        Reminder.objects.filter(pk=r.pk).update(status=ReminderStatus.QUEUED, updated_at=timezone.now())
        assert dispatch_due_reminders() == 0  # too recent to be considered stuck
        clock.tick(timedelta(minutes=16))
        assert dispatch_due_reminders() == 1
        r.refresh_from_db()
        assert r.status == ReminderStatus.SENT


def test_no_reminders_after_payment():
    with freeze_time(T0) as clock:
        req = make_request(paid=True)
        clock.tick(timedelta(days=4))
        assert dispatch_due_reminders() == 0
        assert set(statuses(req)) == {ReminderStatus.CANCELLED}


def test_opted_out_customer_is_skipped():
    with freeze_time(T0) as clock:
        req = make_request()
        req.customer.opted_out_at = timezone.now()
        req.customer.save()
        clock.tick(timedelta(minutes=61))
        sent_before = len(outbox())
        dispatch_due_reminders()
        assert Reminder.objects.filter(follow_up__report_request=req, skip_reason="opted_out").count() == 1
        assert len(outbox()) == sent_before


def test_payment_between_claim_and_send_is_respected():
    with freeze_time(T0) as clock:
        req = make_request()
        clock.tick(timedelta(minutes=61))
        r = Reminder.objects.filter(follow_up__report_request=req).order_by("scheduled_for").first()
        Reminder.objects.filter(pk=r.pk).update(status=ReminderStatus.QUEUED)
        link = req.payment_links.get()
        simulate_payment(link.razorpay_link_id)
        PaymentService().confirm_link_payment(link.razorpay_link_id)  # paid, but follow-up not closed yet
        assert send_reminder(r.pk) == "skipped:paid"


# --- escalation ------------------------------------------------------------------------------------------


def test_escalation_levels_respect_notes_and_cap():
    with freeze_time(T0) as clock:
        req = make_request()
        fu = FollowUp.objects.get(report_request=req)
        clock.tick(timedelta(hours=47))
        assert escalate_overdue_followups() == 0  # not overdue yet
        clock.tick(timedelta(hours=2))  # +49h
        assert escalate_overdue_followups() == 1
        fu.refresh_from_db()
        assert (fu.status, fu.escalation_level, fu.priority) == (FollowUpStatus.ESCALATED, 1, Priority.HIGH)
        assert escalate_overdue_followups() == 0  # same quiet period

        clock.tick(timedelta(hours=11))  # +60h: an agent works on it
        FollowUpNote.objects.create(follow_up=fu, author=UserFactory(), body="Called, will pay tonight")
        clock.tick(timedelta(hours=14))  # +74h: escalated >24h ago but noted 14h ago
        assert escalate_overdue_followups() == 0
        clock.tick(timedelta(hours=11))  # +85h: quiet for 25h
        assert escalate_overdue_followups() == 1
        clock.tick(timedelta(hours=25))
        assert escalate_overdue_followups() == 1
        clock.tick(timedelta(hours=25))
        assert escalate_overdue_followups() == 0  # capped
        fu.refresh_from_db()
        assert fu.escalation_level == settings.FOLLOWUP_MAX_ESCALATION


def test_paid_followups_never_escalate():
    with freeze_time(T0) as clock:
        make_request(paid=True)
        clock.tick(timedelta(hours=72))
        assert escalate_overdue_followups() == 0


# --- expiry & self-healing -------------------------------------------------------------------------------


def test_abandoned_request_expires_and_conversation_resets():
    with freeze_time(T0) as clock:
        req = make_request(generated=False)
        Conversation.objects.filter(customer=req.customer).update(active_report_request=req, state=S.ASK_PAN)
        clock.tick(timedelta(minutes=50))
        Conversation.objects.filter(customer=req.customer).update(
            last_inbound_at=timezone.now()
        )  # still typing
        clock.tick(timedelta(minutes=30))
        assert expire_stale_requests()["abandoned"] == 0
        clock.tick(timedelta(minutes=31))
        assert expire_stale_requests()["abandoned"] == 1
        req.refresh_from_db()
        assert req.report_status == ReportStatus.EXPIRED and req.active_lock is None
        conv = Conversation.objects.get(customer=req.customer)
        assert conv.state == S.ENDED and conv.active_report_request is None
        assert "timed out" in outbox()[-1]["body"]


def test_unpaid_report_expires_with_link_followup_and_reminders():
    with freeze_time(T0) as clock:
        req = make_request()
        clock.tick(timedelta(days=settings.REPORT_VALIDITY_DAYS, minutes=1))
        assert expire_stale_requests()["unpaid_expired"] == 1  # notice can't be sent: window closed, no crash
        req.refresh_from_db()
        assert req.report_status == ReportStatus.EXPIRED
        assert req.payment_links.get().status == PaymentLinkStatus.CANCELLED
        fu = FollowUp.objects.get(report_request=req)
        assert fu.status == FollowUpStatus.CLOSED_LOST and fu.closed_at
        assert not fu.reminders.filter(status=ReminderStatus.SCHEDULED).exists()
        assert dispatch_due_reminders() == 0


def test_stuck_fetch_is_restarted_once():
    with freeze_time(T0) as clock:
        customer = CustomerFactory(pan="ABCDE1234F")
        Conversation.objects.create(customer=customer, last_inbound_at=timezone.now())
        svc = ReportService()
        req, _ = svc.start_request(customer)
        svc.record_consent(req.pk, text_version="v1", text=CONSENT_TEXT)
        svc.initiate_otp(req.pk)
        svc.verify_otp(req.pk, "123456")  # verified, but the fetch task was lost
        clock.tick(timedelta(minutes=11))
        assert expire_stale_requests()["fetch_restarted"] == 1
        req.refresh_from_db()
        assert req.report_status == ReportStatus.GENERATED and req.payment_links.count() == 1
        assert expire_stale_requests()["fetch_restarted"] == 0


# --- retention -------------------------------------------------------------------------------------------


def test_documents_are_purged_after_retention():
    with freeze_time(T0) as clock:
        req = make_request(paid=True)
        doc = req.document
        path = settings.PRIVATE_STORAGE_ROOT / doc.storage_key[:2] / doc.storage_key
        assert path.exists()
        clock.tick(timedelta(days=settings.REPORT_PDF_RETENTION_DAYS - 1))
        assert purge_expired_documents() == 0
        clock.tick(timedelta(days=2))
        assert purge_expired_documents() == 1
        assert not path.exists()
        doc.refresh_from_db()
        assert doc.purged_at and doc.sha256  # metadata kept for the audit trail
        assert purge_expired_documents() == 0
        admin, _ = client_for(Role.ADMIN)
        assert admin.get(f"/api/v1/report-requests/{req.pk}/document").status_code == 409


# --- wiring ----------------------------------------------------------------------------------------------


def test_beat_schedule_only_names_registered_tasks_on_the_right_queues():
    from config.celery import app

    app.loader.import_default_modules()
    for entry in settings.CELERY_BEAT_SCHEDULE.values():
        assert entry["task"] in app.tasks, entry["task"]
    assert app.amqp.router.route({}, "followups.send_reminder")["queue"].name == "reminders"
    assert app.amqp.router.route({}, "reports.expire_stale_requests")["queue"].name == "default"

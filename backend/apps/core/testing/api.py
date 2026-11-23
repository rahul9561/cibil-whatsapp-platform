"""Test helpers for the admin API: role-authenticated clients and data built through the real services."""

from django.test import Client
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import ROLES_REQUIRING_2FA
from apps.conversations.models import Conversation
from apps.followups.models import FollowUp
from apps.payments.gateway.mock import simulate_payment
from apps.reports.services import ReportService

from .factories import CONSENT_TEXT, CustomerFactory, UserFactory


def client_for(role=None, user=None) -> tuple[Client, object]:
    """A logged-in client whose session has passed 2FA where the role requires it."""
    user = user or UserFactory(role=role)
    client = Client()
    client.force_login(user)
    if user.requires_2fa or user.role in ROLES_REQUIRING_2FA:
        device = TOTPDevice.objects.create(user=user, name="test", confirmed=True)
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()
    return client, user


def make_request(*, generated=True, paid=False, assigned_to=None, wa_id=None, pan="ABCDE1234F"):
    """Run a request through the real services (mock adapters). Returns the ReportRequest."""
    from apps.payments.tasks import confirm_payment_task
    from apps.reports.tasks import fetch_report_task, initiate_otp_task

    kwargs = {"pan": pan, "full_name": "Rahul Sharma"}
    if wa_id:
        kwargs["wa_id"] = wa_id
    customer = CustomerFactory(**kwargs)
    Conversation.objects.create(customer=customer, last_inbound_at=timezone.now())
    svc = ReportService()
    req, _ = svc.start_request(customer)
    if not generated:
        return req
    svc.record_consent(req.pk, text_version="v1", text=CONSENT_TEXT)
    initiate_otp_task.delay(str(req.pk))
    svc.verify_otp(req.pk, "123456")
    fetch_report_task.delay(str(req.pk))
    req.refresh_from_db()
    if assigned_to is not None:
        FollowUp.objects.filter(report_request=req).update(assigned_to=assigned_to)
    if paid:
        link = req.payment_links.get()
        simulate_payment(link.razorpay_link_id)
        confirm_payment_task.delay(link.razorpay_link_id)
        req.refresh_from_db()
    return req

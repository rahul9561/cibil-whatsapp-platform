"""WhatsApp conversation bot (ARCHITECTURE §5).

`process_inbound_event` is the single entry point for a customer message:
  decrypt sender -> take the parked text (read-once) -> lock per customer -> log the message (redacted) ->
  run the state handler -> save the conversation -> send replies (idempotent) -> run queued effects.

Effects (e.g. starting the report fetch) run only after the conversation is saved, so a fast background
task can never be overwritten by this handler's state. Replies are cached per message so a retried task
re-sends what is missing without re-running the bot (the text was already consumed).
"""

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.crypto import blind_index, decrypt_str
from apps.core.integrations import LockNotAcquired, PermanentError, TransientError, distributed_lock
from apps.core.masking import mask_name, mask_pan
from apps.customers.models import WA_ID_PURPOSE, Customer
from apps.messaging.gateway.base import Button
from apps.messaging.inbound import take_body
from apps.messaging.models import OTP_PLACEHOLDER, Direction, MessageLog, MessageStatus, MessageType
from apps.messaging.services import MessagingService
from apps.payments.models import PaymentLinkStatus
from apps.payments.services import PaymentService, format_rupees
from apps.reports.models import InvalidTransition, ReportRequest, ReportStatus
from apps.reports.providers.base import VerifyOutcome
from apps.reports.services import OtpResendLimit, ReportService

from . import copy as t
from .models import Conversation
from .models import ConversationState as S
from .validators import clean_dob, clean_name, clean_pan

logger = logging.getLogger(__name__)

IDLE = {S.NEW, S.ENDED, S.DELIVERED, S.REPORT_FAILED, S.NO_HIT}
# States whose replies are personal data: the inbound log keeps a placeholder, never the text.
SENSITIVE_STATES = {
    S.ASK_NAME: "[name]",
    S.ASK_PAN: "[PAN]",
    S.ASK_DOB: "[date of birth]",
    S.AWAITING_OTP: OTP_PLACEHOLDER,
    S.OTP_FAILED: OTP_PLACEHOLDER,
}
_REPLIES_KEY = "wa:replies:"


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)


def _buttons(*pairs) -> list[tuple[str, str]]:
    return list(pairs)


class Bot:
    def __init__(self, customer: Customer, conversation: Conversation):
        self.customer, self.conv = customer, conversation
        self.reports = ReportService()
        self.replies: list[Reply] = []
        self.effects: list = []

    # -- helpers ----------------------------------------------------------------------------------------

    def say(self, text: str, *buttons):
        self.replies.append(Reply(text, _buttons(*buttons)))

    def go(self, state: str):
        self.conv.set_state(state)

    @property
    def request(self) -> ReportRequest | None:
        req = self.conv.active_report_request
        if req is not None:
            req.refresh_from_db()
        return req

    # -- entry ------------------------------------------------------------------------------------------

    def handle(self, text: str, reply_id: str | None, wa_message_id: str) -> list[Reply]:
        self.wa_message_id = wa_message_id
        choice = (reply_id or text or "").strip()
        command = choice.upper()
        if command in {"HELP", "MENU"}:
            self.say(t.HELP)
        elif command == "STOP":
            self.customer.opted_out_at = self.customer.opted_out_at or timezone.now()
            self.customer.save(update_fields=["opted_out_at", "updated_at"])
            self.say(t.OPTED_OUT)
        elif command == "START" and self.customer.is_opted_out:
            self.customer.opted_out_at = None
            self.customer.save(update_fields=["opted_out_at", "updated_at"])
            self.say(t.OPTED_IN)
        elif command in {"CANCEL", "RESTART"}:
            self.cancel()
        else:
            getattr(self, f"on_{self.conv.state}")(choice, reply_id)
        return self.replies

    def cancel(self):
        req = self.request
        if req is not None and req.is_open:
            try:
                self.reports.cancel(req.pk, by_user=None, reason="customer_cancelled_on_whatsapp")
            except InvalidTransition:
                self.say(t.CANNOT_CANCEL)
                return
        self.conv.active_report_request = None
        self.go(S.ENDED)
        self.say(t.CANCELLED)

    # -- idle / start -----------------------------------------------------------------------------------

    def _idle(self, choice, reply_id):
        try:
            req, created = self.reports.start_request(self.customer)
        except PermanentError:
            self.say(t.UNAVAILABLE)
            return
        self.conv.active_report_request = req
        if created:
            self.say(t.WELCOME)
            self.start_details()
        else:
            self.resume(req)

    on_new = on_ended = on_delivered = on_report_failed = on_no_hit = _idle

    def start_details(self):
        c = self.customer
        if c.full_name and c.pan and c.dob:
            self.say(t.SAVED_DETAILS)
            self.prompt_confirm()
        else:
            self.go(S.ASK_NAME)
            self.say(t.ASK_NAME)

    def resume(self, req: ReportRequest):
        status = req.report_status
        if status == ReportStatus.DRAFT:
            self.start_details()
        elif status == ReportStatus.CONSENTED:
            self.request_otp()
        elif status == ReportStatus.OTP_SENT:
            self.go(S.AWAITING_OTP)
            self.say(t.OTP_SENT.format(destination=""), t.BTN_RESEND)
        elif status in {ReportStatus.OTP_VERIFIED, ReportStatus.FETCHING}:
            self.go(S.FETCHING_REPORT)
            self.say(t.STILL_FETCHING)
        elif status == ReportStatus.GENERATED:
            if req.payment_links.filter(status=PaymentLinkStatus.PAID).exists():
                self.say(t.PAYMENT_RECEIVED)  # paid, delivery in progress: never ask for payment again
                return
            self.go(S.AWAITING_PAYMENT)
            self.on_awaiting_payment("", None)

    # -- details ----------------------------------------------------------------------------------------

    def on_ask_name(self, choice, reply_id):
        name = clean_name(choice)
        if not name:
            return self.say(t.INVALID_NAME)
        self.customer.full_name = name
        self.customer.save(update_fields=["full_name", "updated_at"])
        self.go(S.ASK_PAN)
        self.say(t.ASK_PAN)

    def on_ask_pan(self, choice, reply_id):
        pan = clean_pan(choice)
        if not pan:
            return self.say(t.INVALID_PAN)
        self.customer.set_pan(pan)
        self.customer.save(update_fields=["pan", "pan_hash", "updated_at"])
        self.go(S.ASK_DOB)
        self.say(t.ASK_DOB)

    def on_ask_dob(self, choice, reply_id):
        dob = clean_dob(choice)
        if not dob:
            return self.say(t.INVALID_DOB)
        self.customer.dob = dob.isoformat()
        self.customer.save(update_fields=["dob", "updated_at"])
        self.prompt_confirm()

    def on_ask_extra(self, choice, reply_id):  # reserved for provider-specific fields (Phase 10)
        self.prompt_confirm()

    def prompt_confirm(self):
        c = self.customer
        dob = c.dob[8:10] + "-" + c.dob[5:7] + "-****" if c.dob else ""
        self.go(S.CONFIRM_DETAILS)
        self.say(
            t.CONFIRM_DETAILS.format(name=mask_name(c.full_name), pan=mask_pan(c.pan), dob=dob),
            t.BTN_CONFIRM,
            t.BTN_EDIT,
        )

    def on_confirm_details(self, choice, reply_id):
        answer = choice.lower()
        if answer in {"confirm", "yes", "y", "1"}:
            self.go(S.AWAITING_CONSENT)
            self.say(
                t.CONSENT_TEXT.format(
                    price=format_rupees(settings.REPORT_PRICE_PAISE),
                    retention=settings.REPORT_PDF_RETENTION_DAYS,
                ),
                t.BTN_AGREE,
                t.BTN_DECLINE,
            )
        elif answer in {"edit", "no", "n", "2"}:
            self.go(S.ASK_NAME)
            self.say(t.ASK_NAME)
        else:
            self.say(t.PLEASE_CHOOSE, t.BTN_CONFIRM, t.BTN_EDIT)

    # -- consent & OTP ----------------------------------------------------------------------------------

    def on_awaiting_consent(self, choice, reply_id):
        answer = choice.lower()
        if answer in {"agree", "yes", "i agree", "y"}:
            text = t.CONSENT_TEXT.format(
                price=format_rupees(settings.REPORT_PRICE_PAISE), retention=settings.REPORT_PDF_RETENTION_DAYS
            )
            self.reports.record_consent(
                self.request.pk, text_version=t.CONSENT_VERSION, text=text, wa_message_id=self.wa_message_id
            )
            self.request_otp()
        elif answer in {"decline", "no", "n", "no, thanks"}:
            req = self.request
            if req is not None and req.is_open:
                self.reports.cancel(req.pk, by_user=None, reason="consent_declined")
            self.conv.active_report_request = None
            self.go(S.ENDED)
            self.say(t.DECLINED)
        else:
            self.say(t.PLEASE_CHOOSE, t.BTN_AGREE, t.BTN_DECLINE)

    def request_otp(self):
        req = self.request
        resend = req.report_status == ReportStatus.OTP_SENT
        try:
            session = self.reports.initiate_otp(req.pk)
        except OtpResendLimit:
            self.go(S.AWAITING_OTP)
            return self.say(t.OTP_RESEND_LIMIT)
        except (TransientError, LockNotAcquired):
            self.go(S.OTP_FAILED)
            return self.say(t.OTP_TRY_AGAIN, t.BTN_RETRY)
        except PermanentError as exc:
            self.reports.mark_failed(req.pk, exc.code)
            self.go(S.REPORT_FAILED)
            return self.say(t.REJECTED)
        self.go(S.AWAITING_OTP)
        if resend:
            self.say(t.OTP_RESENT, t.BTN_RESEND)
        else:
            self.say(t.OTP_SENT.format(destination=session.masked_destination), t.BTN_RESEND)

    def on_otp_failed(self, choice, reply_id):
        self.request_otp()

    def on_otp_requesting(self, choice, reply_id):
        self.request_otp()

    def on_awaiting_otp(self, choice, reply_id):
        req = self.request
        if req is None or req.report_status != ReportStatus.OTP_SENT:
            return self.resume(req) if req else self._idle(choice, reply_id)
        if (reply_id or choice.upper()) in {"resend", "RESEND"}:
            return self.request_otp()
        if not choice.isdigit():
            return self.say(t.OTP_NOT_NUMERIC, t.BTN_RESEND)
        try:
            outcome = self.reports.verify_otp(req.pk, choice)
        except (TransientError, LockNotAcquired):
            return self.say(t.OTP_VERIFY_LATER)
        req.refresh_from_db()
        if outcome == VerifyOutcome.VERIFIED:
            self.go(S.FETCHING_REPORT)
            self.say(t.FETCHING)
            from apps.reports.tasks import fetch_report_task

            self.effects.append(lambda pk=str(req.pk): fetch_report_task.delay(pk))
        elif req.report_status == ReportStatus.FAILED:
            self.go(S.REPORT_FAILED)
            self.say(t.OTP_TOO_MANY)
        elif outcome == VerifyOutcome.EXPIRED:
            self.say(t.OTP_EXPIRED, t.BTN_RESEND)
        else:
            left = max(settings.OTP_MAX_ATTEMPTS - req.otp_attempts, 0)
            self.say(t.OTP_INVALID.format(left=left), t.BTN_RESEND)

    # -- after verification -----------------------------------------------------------------------------

    def on_fetching_report(self, choice, reply_id):
        req = self.request
        if req is not None and req.report_status not in {ReportStatus.OTP_VERIFIED, ReportStatus.FETCHING}:
            return self.resume(req) if req.is_open else self._idle(choice, reply_id)
        self.say(t.STILL_FETCHING)

    def on_awaiting_payment(self, choice, reply_id):
        req = self.request
        if req is None or not req.is_open:
            return self._idle(choice, reply_id)
        if req.payment_links.filter(status=PaymentLinkStatus.PAID).exists():
            return self.say(t.PAYMENT_RECEIVED)
        try:
            link = PaymentService().get_or_create_link(req.pk)
        except (TransientError, PermanentError):
            return self.say(t.UNAVAILABLE)
        self.say(
            t.PAYMENT_REMINDER.format(
                ref=req.public_ref, amount=format_rupees(link.amount_paise), url=link.short_url
            )
        )


# -- entry point ------------------------------------------------------------------------------------------


def _customer_for(wa_id: str) -> Customer:
    customer = Customer.objects.by_wa_id(wa_id).first()
    if customer:
        return customer
    customer = Customer()
    customer.set_wa_id(wa_id)
    try:
        with transaction.atomic():
            customer.save()
    except IntegrityError:
        customer = Customer.objects.by_wa_id(wa_id).get()
    return customer


def _redacted_body(state: str, text: str | None, reply_id: str | None, msg_type: str) -> str:
    if reply_id:
        return f"[button] {reply_id}"
    if text is None:
        return f"[{msg_type}]"
    if state in SENSITIVE_STATES:
        return SENSITIVE_STATES[state]
    from apps.core.redaction import scrub

    return scrub(text)[:1000]


def _send(customer, conv, replies: list[Reply], wa_message_id: str):
    messaging = MessagingService()
    for i, reply in enumerate(replies):
        messaging.send(
            customer,
            text=reply.text,
            buttons=[Button(*b) for b in reply.buttons] or None,
            idempotency_key=f"reply:{wa_message_id}:{i}",
            related_request=conv.active_report_request,
        )


def process_inbound_event(event) -> str:
    data = event.payload
    wa_message_id, msg_type, reply_id = (
        data["wa_message_id"],
        data.get("type", ""),
        data.get("reply_id") or None,
    )
    wa_id = decrypt_str(data["from_enc"])
    text = take_body(wa_message_id)

    with distributed_lock(f"wa-customer:{blind_index(wa_id, WA_ID_PURPOSE)}", timeout=120):
        customer = _customer_for(wa_id)
        conv, _ = Conversation.objects.select_related("active_report_request").get_or_create(
            customer=customer
        )
        try:
            with transaction.atomic():
                MessageLog.objects.create(
                    customer=customer,
                    direction=Direction.INBOUND,
                    wa_message_id=wa_message_id,
                    type=MessageType.INTERACTIVE
                    if reply_id
                    else MessageType.TEXT
                    if msg_type == "text"
                    else MessageType.OTHER,
                    body_redacted=_redacted_body(conv.state, text, reply_id, msg_type),
                    status=MessageStatus.RECEIVED,
                    related_report_request=conv.active_report_request,
                )
        except IntegrityError:  # retry of a message we already handled: only re-send missing replies
            cached = cache.get(_REPLIES_KEY + wa_message_id) or []
            _send(customer, conv, [Reply(**r) for r in cached], wa_message_id)
            return "resent"

        conv.last_inbound_at = timezone.now()
        bot = Bot(customer, conv)
        if msg_type not in {"text", "interactive", "button"}:
            bot.say(t.UNSUPPORTED)
        elif text is None and reply_id is None:
            bot.say(t.LOST_MESSAGE)
        else:
            bot.handle(text or "", reply_id, wa_message_id)
        conv.save()

        cache.set(_REPLIES_KEY + wa_message_id, [r.__dict__ for r in bot.replies], timeout=60 * 60)
        _send(customer, conv, bot.replies, wa_message_id)
    for effect in bot.effects:
        effect()
    return conv.state

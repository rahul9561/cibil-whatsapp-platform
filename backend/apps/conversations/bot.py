"""WhatsApp conversation bot (ARCHITECTURE §5).

`process_inbound_event` is the single entry point for a customer message:
  decrypt sender -> take the parked text (read-once) -> lock per customer -> log the message (redacted) ->
  run the state handler -> save the conversation -> send replies (idempotent) -> run queued effects.

Effects (e.g. starting the report fetch) run only after the conversation is saved, so a fast background
task can never be overwritten by this handler's state. Replies are cached per message so a retried task
re-sends what is missing without re-running the bot (the text was already consumed).
"""

import hmac
import json
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import service as audit
from apps.audit.models import ActorType
from apps.core.crypto import blind_index, decrypt_str, encrypt_str
from apps.core.integrations import LockNotAcquired, PermanentError, TransientError, distributed_lock
from apps.core.masking import mask_name, mask_pan, mask_phone
from apps.customers.models import WA_ID_PURPOSE, Customer, Gender
from apps.messaging.gateway.base import Button
from apps.messaging.inbound import take_body
from apps.messaging.models import OTP_PLACEHOLDER, Direction, MessageLog, MessageStatus, MessageType
from apps.messaging.services import MessagingService
from apps.payments.models import PaymentLinkStatus
from apps.payments.services import PaymentService, format_rupees
from apps.reports.models import InvalidTransition, ReportRequest, ReportStatus
from apps.reports.providers.base import VerifyOutcome
from apps.reports.services import OtpResendLimit, PrefillLimitReached, ReportService

from . import copy as t
from .models import Conversation
from .models import ConversationState as S
from .validators import clean_dob, clean_mobile, clean_name, clean_pan

logger = logging.getLogger(__name__)

IDLE = {S.NEW, S.ENDED, S.DELIVERED, S.REPORT_FAILED, S.NO_HIT}
# States whose replies are personal data: the inbound log keeps a placeholder, never the text.
SENSITIVE_STATES = {
    S.ASK_NAME: "[name]",
    S.ASK_PAN: "[PAN]",
    S.ASK_DOB: "[date of birth]",
    S.ASK_ALT_MOBILE: "[mobile]",
    S.VERIFY_ALT_PAN: "[PAN]",
    S.AWAITING_OTP: OTP_PLACEHOLDER,
    S.OTP_FAILED: OTP_PLACEHOLDER,
}
_REPLIES_KEY = "wa:replies:"


START_WORDS = {"hi", "hii", "hello", "hey", "namaste", "start", "report", "cibil", "new", "yes", "menu"}


def wants_new_report(text: str) -> bool:
    words = (text or "").lower().replace("!", " ").replace(".", " ").replace("?", " ").split()
    return bool(words) and (words[0] in START_WORDS or "report" in words)


PENDING_TTL = 30 * 60


def _pending_key(customer) -> str:
    return f"prefill-pending:{customer.pk}"


def save_pending(customer, data: dict) -> None:
    """Looked-up details wait here (encrypted, 30 min), never in the database, until the customer confirms.
    Keys: full_name, pan, gender; for an alternate number also mobile and verified (PAN matched)."""
    cache.set(_pending_key(customer), encrypt_str(json.dumps(data)), timeout=PENDING_TTL)


def load_pending(customer) -> dict | None:
    blob = cache.get(_pending_key(customer))
    return json.loads(decrypt_str(blob)) if blob else None


def is_unverified_alternate(pending: dict | None) -> bool:
    """Details found for a number the customer typed are hidden until they type the matching PAN."""
    return bool(pending and pending.get("mobile") and not pending.get("verified"))


# Alternate mobile numbers a customer may try per request (each lookup also counts toward
# PREFILL_MAX_PER_DAY). Tried numbers are kept only as keyed hashes in the conversation context.
ALT_MOBILE_MAX_TRIES = 2
ALT_MOBILE_PURPOSE = "conversation.alt_mobile"


def clear_pending(customer) -> None:
    cache.delete(_pending_key(customer))


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
        clear_pending(self.customer)
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

    @property
    def prefill_mode(self) -> bool:
        p = self.reports.provider
        return bool(settings.PREFILL_BY_MOBILE and p.supports_prefill and not p.requires_otp)

    def details_complete(self) -> bool:
        return all(getattr(self.customer, f) for f in self.needed)

    def ask_consent_first(self):
        """Prefill flow: consent before the bureau lookup."""
        self.conv.context = {**self.conv.context, "details_confirmed": False}
        self.go(S.AWAITING_CONSENT)
        self.say(self.consent()[1], t.BTN_AGREE, t.BTN_DECLINE)

    on_new = _idle  # first contact: any message starts

    def _after_request(self, choice, reply_id):
        """After a finished/cancelled/failed request, only a clear intent starts a new (paid) request.

        "thank you" after receiving a report must not open a new purchase flow.
        """
        has_open = ReportRequest.objects.filter(active_lock=self.customer.pk).exists()
        if has_open or wants_new_report(choice):
            return self._idle(choice, reply_id)
        self.say(t.IDLE_HINT)

    on_ended = on_delivered = on_report_failed = on_no_hit = _after_request

    # Customer details in the order they are asked; only the provider's required fields are asked.
    FIELD_STEPS = (
        ("full_name", S.ASK_NAME, t.ASK_NAME),
        ("pan", S.ASK_PAN, t.ASK_PAN),
        ("dob", S.ASK_DOB, t.ASK_DOB),
        ("gender", S.ASK_EXTRA, t.ASK_GENDER),
    )

    @property
    def needed(self) -> frozenset[str]:
        return self.reports.provider.required_fields

    def _ask(self, field: str):
        for name, state, prompt in self.FIELD_STEPS:
            if name == field:
                self.go(state)
                buttons = (t.BTN_MALE, t.BTN_FEMALE) if name == "gender" else ()
                return self.say(prompt, *buttons)
        return None

    def ask_next_after(self, field: str | None):
        """Ask the next required field after `field` (None = from the start), else confirm."""
        names = [name for name, _, _ in self.FIELD_STEPS if name in self.needed]
        later = names[names.index(field) + 1 :] if field in names else names
        if later:
            return self._ask(later[0])
        return self.prompt_confirm()

    def start_details(self):
        if self.details_complete():
            self.say(t.SAVED_DETAILS)
            self.prompt_confirm()
        elif self.prefill_mode:
            self.ask_consent_first()
        else:
            self.ask_next_after(None)

    def resume(self, req: ReportRequest):
        status = req.report_status
        if status == ReportStatus.DRAFT:
            self.start_details()
        elif status == ReportStatus.CONSENTED:
            if self.reports.provider.requires_otp:
                self.request_otp()
            elif self.conv.state == S.FETCHING_REPORT:
                self.start_direct_fetch(req, t.STILL_FETCHING)
            elif is_unverified_alternate(load_pending(self.customer)):
                self.ask_alt_pan(mask_phone(load_pending(self.customer)["mobile"]))
            elif load_pending(self.customer):
                self.prompt_confirm(pending=load_pending(self.customer))
            elif self.details_complete():
                self.prompt_confirm()
            else:
                self.run_prefill()
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
        self.ask_next_after("full_name")

    def on_ask_pan(self, choice, reply_id):
        pan = clean_pan(choice)
        if not pan:
            return self.say(t.INVALID_PAN)
        self.customer.set_pan(pan)
        self.customer.save(update_fields=["pan", "pan_hash", "updated_at"])
        self.ask_next_after("pan")

    def on_ask_dob(self, choice, reply_id):
        dob = clean_dob(choice)
        if not dob:
            return self.say(t.INVALID_DOB)
        self.customer.dob = dob.isoformat()
        self.customer.save(update_fields=["dob", "updated_at"])
        self.ask_next_after("dob")

    def on_ask_extra(self, choice, reply_id):  # gender (asked only when the provider needs it)
        gender = {"male": "M", "m": "M", "female": "F", "f": "F"}.get((reply_id or choice).strip().lower())
        if not gender:
            return self.say(t.INVALID_GENDER, t.BTN_MALE, t.BTN_FEMALE)
        self.customer.gender = gender
        self.customer.save(update_fields=["gender", "updated_at"])
        self.ask_next_after("gender")

    def prompt_confirm(self, pending: dict | None = None):
        c = self.customer
        name = pending["full_name"] if pending else c.full_name
        pan = pending["pan"] if pending else c.pan
        gender = pending["gender"] if pending else c.gender
        lines = [f"Name: {mask_name(name)}", f"PAN: {mask_pan(pan)}"]
        if "dob" in self.needed and c.dob and not pending:
            lines.append(f"Date of birth: {c.dob[8:10]}-{c.dob[5:7]}-****")
        if "gender" in self.needed and gender:
            lines.append(f"Gender: {dict(Gender.choices).get(gender, gender)}")
        self.go(S.CONFIRM_DETAILS)
        if pending:
            self.say(t.CONFIRM_PREFILL.format(details="\n".join(lines)), t.BTN_ITS_ME, t.BTN_NOT_ME)
        else:
            self.say(t.CONFIRM_DETAILS.format(details="\n".join(lines)), t.BTN_CONFIRM, t.BTN_EDIT)

    def run_prefill(self, mobile: str | None = None):
        """After consent: look up the details linked to the WhatsApp number (or an alternate number the
        customer sent). Nothing found -> offer another number; provider problem -> ask the customer."""
        try:
            found = self.reports.prefill(self.request.pk, mobile)
        except PrefillLimitReached:
            return self.type_details(t.PREFILL_LIMIT)
        except (TransientError, PermanentError, LockNotAcquired):
            return self.type_details(t.PREFILL_UNAVAILABLE)
        if mobile:
            return self._alternate_result(mobile, found)
        if found is None:
            return self.offer_alt_mobile(t.OWN_NUMBER_NOT_FOUND.format(mobile=self.customer.masked_phone))
        save_pending(self.customer, {"full_name": found.full_name, "pan": found.pan, "gender": found.gender})
        self.show_pending(load_pending(self.customer))

    def show_pending(self, pending: dict):
        missing = [f for f in ("full_name", "pan", "gender") if f in self.needed and not pending.get(f)]
        if missing:
            # Partial match: keep what was found and let the customer fill the gap (saved on confirm).
            self.apply_pending(pending)
            clear_pending(self.customer)
            self.say(t.PREFILL_NOT_FOUND)
            return self._ask(missing[0])
        self.prompt_confirm(pending=pending)

    def type_details(self, message: str = t.TYPE_DETAILS):
        clear_pending(self.customer)
        self.say(message)
        self.ask_next_after(None)

    # -- alternate mobile number ------------------------------------------------------------------------

    def alt_tries_left(self) -> int:
        return ALT_MOBILE_MAX_TRIES - len(self.conv.context.get("alt_tried", []))

    def offer_alt_mobile(self, intro: str):
        """Ask for another number (the customer can also choose to type their details instead)."""
        if self.alt_tries_left() <= 0:
            return self.type_details(f"{intro}\n\n{t.ALT_NO_MORE_TRIES}")
        self.go(S.ASK_ALT_MOBILE)
        self.say(f"{intro}\n\n{t.ASK_ALT_MOBILE}", t.BTN_TYPE_DETAILS)

    def on_ask_alt_mobile(self, choice, reply_id):
        if (reply_id or choice.lower()) in {"type_details", "type my details"}:
            return self.type_details()
        mobile = clean_mobile(choice)
        if not mobile:
            return self.say(t.INVALID_MOBILE, t.BTN_TYPE_DETAILS)
        tried = list(self.conv.context.get("alt_tried", []))
        marker = blind_index(mobile, ALT_MOBILE_PURPOSE)
        if mobile == self.customer.wa_id or marker in tried:
            return self.say(t.ALT_ALREADY_TRIED, t.BTN_TYPE_DETAILS)
        self.conv.context = {**self.conv.context, "alt_tried": [*tried, marker]}
        self.run_prefill(mobile)

    def _alternate_result(self, mobile: str, found):
        masked = mask_phone(mobile)
        if found is None or not found.pan:  # without a PAN there is nothing to prove ownership with
            return self.offer_alt_mobile(t.ALT_NOT_FOUND.format(mobile=masked))
        pending = {"full_name": found.full_name, "pan": found.pan, "gender": found.gender}
        save_pending(self.customer, {**pending, "mobile": mobile, "verified": False})
        self.ask_alt_pan(masked)

    def ask_alt_pan(self, masked_mobile: str):
        # Nothing from the lookup is shown yet: a stranger's number must not reveal whose it is.
        self.go(S.VERIFY_ALT_PAN)
        self.say(t.ALT_VERIFY_PAN.format(mobile=masked_mobile), t.BTN_TYPE_DETAILS)

    def on_verify_alt_pan(self, choice, reply_id):
        if (reply_id or choice.lower()) in {"type_details", "type my details"}:
            return self.type_details()
        pending = load_pending(self.customer)
        if not pending or not pending.get("mobile"):  # expired (30 min): start the typed route
            return self.type_details(t.PREFILL_NOT_FOUND)
        pan = clean_pan(choice)
        if not pan:
            return self.say(t.INVALID_PAN, t.BTN_TYPE_DETAILS)
        if not hmac.compare_digest(pan, pending.get("pan") or ""):
            # One chance only: retrying would let someone guess a PAN against the bureau's record.
            audit.record("report.alternate_mobile_rejected", actor_type=ActorType.SYSTEM, obj=self.request)
            return self.type_details(t.ALT_PAN_MISMATCH.format(mobile=mask_phone(pending["mobile"])))
        pending["verified"] = True
        save_pending(self.customer, pending)
        self.show_pending(pending)

    def apply_pending(self, pending: dict):
        c = self.customer
        c.full_name = pending.get("full_name") or c.full_name
        if pending.get("pan"):
            c.set_pan(pending["pan"])
        c.gender = pending.get("gender") or c.gender
        c.save(update_fields=["full_name", "pan", "pan_hash", "gender", "updated_at"])
        if pending.get("mobile") and pending.get("verified"):
            self.reports.use_alternate_mobile(self.request.pk, pending["mobile"])

    def consent(self) -> tuple[str, str]:
        """(version, exact text) for the active provider: the OTP promise only appears when it is true."""
        direct = not self.reports.provider.requires_otp
        prefill = self.prefill_mode and not self.conv.context.get("details_confirmed")
        if prefill:
            verification = t.CONSENT_PREFILL_LINE.format(mobile=self.customer.masked_phone)
        elif direct:
            verification = t.CONSENT_DIRECT_LINE.format(mobile=self.customer.masked_phone)
        else:
            verification = t.CONSENT_OTP_LINE
        text = t.CONSENT_TEXT.format(
            verification=verification,
            price=format_rupees(settings.REPORT_PRICE_PAISE),
            retention=settings.REPORT_PDF_RETENTION_DAYS,
        )
        flavour = "prefill" if prefill else "direct" if direct else "otp"
        return f"{t.CONSENT_VERSION}-{flavour}", text

    def on_confirm_details(self, choice, reply_id):
        answer = choice.lower()
        pending = load_pending(self.customer)
        if is_unverified_alternate(pending):
            pending = None  # never confirmed without the PAN check
        req = self.request
        consented = bool(req and req.report_status == ReportStatus.CONSENTED)
        if answer in {"confirm", "yes", "y", "1", "yes, that's me"}:
            if pending:
                self.apply_pending(pending)  # only now do looked-up details enter the customer record
                clear_pending(self.customer)
            if consented and not self.reports.provider.requires_otp:
                return self.start_direct_fetch(req, t.FETCHING_DIRECT)
            self.conv.context = {**self.conv.context, "details_confirmed": True}
            self.go(S.AWAITING_CONSENT)
            self.say(self.consent()[1], t.BTN_AGREE, t.BTN_DECLINE)
        elif answer in {"not_me", "no, not me"} or (pending and answer in {"edit", "no", "n", "2"}):
            clear_pending(self.customer)
            from_whatsapp_number = pending is not None and not pending.get("mobile")
            if from_whatsapp_number and consented and self.prefill_mode and self.alt_tries_left() > 0:
                return self.offer_alt_mobile(t.NOT_ME_OFFER_ALT)  # e.g. a recycled number
            self.say(t.PREFILL_NOT_ME)
            self.ask_next_after(None)
        elif answer in {"edit", "no", "n", "2"}:
            self.ask_next_after(None)
        elif pending:
            self.say(t.PLEASE_CHOOSE, t.BTN_ITS_ME, t.BTN_NOT_ME)
        else:
            self.say(t.PLEASE_CHOOSE, t.BTN_CONFIRM, t.BTN_EDIT)

    # -- consent & OTP ----------------------------------------------------------------------------------

    def on_awaiting_consent(self, choice, reply_id):
        answer = choice.lower()
        if answer in {"agree", "yes", "i agree", "y"}:
            version, text = self.consent()
            self.reports.record_consent(
                self.request.pk, text_version=version, text=text, wa_message_id=self.wa_message_id
            )
            if self.reports.provider.requires_otp:
                self.request_otp()
            elif self.conv.context.get("details_confirmed") or not self.prefill_mode:
                self.start_direct_fetch(self.request, t.FETCHING_DIRECT)
            else:
                self.run_prefill()
        elif answer in {"decline", "no", "n", "no, thanks"}:
            req = self.request
            if req is not None and req.is_open:
                self.reports.cancel(req.pk, by_user=None, reason="consent_declined")
            clear_pending(self.customer)
            self.conv.active_report_request = None
            self.go(S.ENDED)
            self.say(t.DECLINED)
        else:
            self.say(t.PLEASE_CHOOSE, t.BTN_AGREE, t.BTN_DECLINE)

    def start_direct_fetch(self, req: ReportRequest, message: str):
        """Providers without OTP: the report is requested as soon as consent is recorded."""
        from apps.reports.tasks import fetch_report_task

        self.conv.context = {k: v for k, v in self.conv.context.items() if k != "details_confirmed"}
        self.go(S.FETCHING_REPORT)
        self.say(message)
        self.effects.append(lambda pk=str(req.pk): fetch_report_task.delay(pk))

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
            return self.resume(req) if req else self._after_request(choice, reply_id)
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
            return self.resume(req) if req.is_open else self._after_request(choice, reply_id)
        self.say(t.STILL_FETCHING)

    def on_awaiting_payment(self, choice, reply_id):
        req = self.request
        if req is None or not req.is_open:
            return self._after_request(choice, reply_id)
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

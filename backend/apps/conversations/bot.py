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
from apps.core.masking import mask_pan_digits, mask_phone
from apps.customers.models import WA_ID_PURPOSE, Customer, Gender
from apps.messaging.gateway.base import Button
from apps.messaging.inbound import take_body
from apps.messaging.models import OTP_PLACEHOLDER, Direction, MessageLog, MessageStatus, MessageType
from apps.messaging.services import MessagingService
from apps.payments.models import PaymentLinkStatus
from apps.payments.services import PaymentService, format_rupees
from apps.reports.models import InvalidTransition, ReportRequest, ReportStatus
from apps.reports.providers import get_prefill_provider
from apps.reports.providers.base import VerifyOutcome
from apps.reports.services import OtpResendLimit, PrefillLimitReached, ReportService

from . import copy as _en
from .i18n import LANGUAGE_BUTTON_PREFIX, LANGUAGE_COMMANDS, LANGUAGE_MENU, copy_for, parse_language
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

# Reset the chat: typed (any case) or the "Start over" button.
RESET_COMMANDS = {"RESET", "RESET CHAT", "RESTART", "START OVER", "STARTOVER", "NEW CHAT"}
RESET_BUTTON_ID = "reset"
# Only requests that haven't reached the bureau yet are thrown away by a reset.
RESETTABLE = {ReportStatus.DRAFT, ReportStatus.CONSENTED, ReportStatus.OTP_SENT}
ALT_MOBILE_PURPOSE = "conversation.alt_mobile"


def clear_pending(customer) -> None:
    cache.delete(_pending_key(customer))


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    list_button: str = ""  # set: more than 3 choices, sent as a list menu opened by this label


def _buttons(*pairs) -> list[tuple[str, str]]:
    return list(pairs)


class Bot:
    def __init__(self, customer: Customer, conversation: Conversation):
        self.customer, self.conv = customer, conversation
        self.t = copy_for(customer.language)  # every message in the customer's language
        self.reports = ReportService()
        self.replies: list[Reply] = []
        self.effects: list = []

    # -- helpers ----------------------------------------------------------------------------------------

    def say(self, text: str, *buttons, list_button: str = ""):
        self.replies.append(Reply(text, _buttons(*buttons), list_button))

    # -- language ---------------------------------------------------------------------------------------

    @property
    def needs_language(self) -> bool:
        return settings.WHATSAPP_ASK_LANGUAGE and not self.customer.language

    def ask_language(self):
        rows = [(f"{LANGUAGE_BUTTON_PREFIX}{code}", label) for code, label in LANGUAGE_MENU]
        self.say(_en.CHOOSE_LANGUAGE, *rows, list_button=_en.LANGUAGE_LIST_BUTTON)

    def choose_language(self, choice: str, reply_id: str | None) -> None:
        """Until a language is chosen every message gets the menu; then the flow continues in it."""
        code = parse_language(reply_id, choice)
        if code is None:
            return self.ask_language()
        changing = self.conv.context.get("changing_language")
        self.customer.language = code
        self.customer.save(update_fields=["language", "updated_at"])
        self.t = copy_for(code)
        self.conv.context = {k: v for k, v in self.conv.context.items() if k != "changing_language"}
        req = self.request
        if changing:  # LANGUAGE command: confirm, then pick up where the customer was
            self.say(self.t.LANGUAGE_SET)
            if req is not None and req.is_open:
                self.resume(req)
            return None
        if self.conv.state in IDLE or req is None:  # first contact: straight into the flow
            return self._idle(choice, None)
        return self.resume(req)  # existing customer from before languages existed

    def change_language(self):
        self.customer.language = ""
        self.customer.save(update_fields=["language", "updated_at"])
        self.conv.context = {**self.conv.context, "changing_language": True}
        self.ask_language()

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
        if command in LANGUAGE_COMMANDS and settings.WHATSAPP_ASK_LANGUAGE:
            self.change_language()
        elif self.needs_language:
            self.choose_language(choice, reply_id)
        elif command in {"HELP", "MENU"}:
            self.say(self.t.HELP)
        elif command == "STOP":
            self.customer.opted_out_at = self.customer.opted_out_at or timezone.now()
            self.customer.save(update_fields=["opted_out_at", "updated_at"])
            self.say(self.t.OPTED_OUT)
        elif command == "START" and self.customer.is_opted_out:
            self.customer.opted_out_at = None
            self.customer.save(update_fields=["opted_out_at", "updated_at"])
            self.say(self.t.OPTED_IN)
        elif command in RESET_COMMANDS or reply_id == RESET_BUTTON_ID:
            self.reset()
        elif command == "CANCEL":
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
                self.say(self.t.CANNOT_CANCEL)
                return
        clear_pending(self.customer)
        self.conv.active_report_request = None
        self.go(S.ENDED)
        self.say(self.t.CANCELLED)

    def reset(self):
        """Start the chat from scratch: cancel the open request, forget looked-up and typed details.

        A report already being fetched or paid for is never thrown away (same rule as CANCEL). The chosen
        language stays; LANGUAGE changes it.
        """
        req = self.request
        if req is not None and req.is_open:
            if req.report_status not in RESETTABLE:
                # Already pulled from the bureau (paid for) or being pulled: keep it, show where it stands.
                self.say(self.t.CANNOT_CANCEL)
                return self.resume(req)
            try:
                self.reports.cancel(req.pk, by_user=None, reason="customer_reset_chat")
            except InvalidTransition:
                return self.say(self.t.CANNOT_CANCEL)
        clear_pending(self.customer)
        c = self.customer
        c.full_name, c.dob, c.gender = "", "", ""
        c.set_pan("")
        c.save(update_fields=["full_name", "dob", "gender", "pan", "pan_hash", "updated_at"])
        self.conv.active_report_request = None
        self.conv.context = {}
        self.go(S.NEW)
        audit.record("conversation.reset", actor_type=ActorType.SYSTEM, obj=c)
        return self.say(self.t.RESET_DONE, self.t.BTN_START_AGAIN)

    # -- idle / start -----------------------------------------------------------------------------------

    def _idle(self, choice, reply_id):
        try:
            req, created = self.reports.start_request(self.customer)
        except PermanentError:
            self.say(self.t.UNAVAILABLE)
            return
        self.conv.active_report_request = req
        if created:
            self.say(self.t.WELCOME)
            self.start_details()
        else:
            self.resume(req)

    @property
    def prefill_mode(self) -> bool:
        # Prefill (one provider) only feeds report providers without OTP (another provider).
        if not settings.PREFILL_BY_MOBILE or self.reports.provider.requires_otp:
            return False
        return get_prefill_provider() is not None

    def details_complete(self) -> bool:
        return all(getattr(self.customer, f) for f in self.needed)

    def ask_consent_first(self):
        """Prefill flow: consent before the bureau lookup."""
        self.conv.context = {**self.conv.context, "details_confirmed": False}
        self.go(S.AWAITING_CONSENT)
        self.say(self.consent()[1], self.t.BTN_AGREE, self.t.BTN_DECLINE)

    on_new = _idle  # first contact: any message starts

    def _after_request(self, choice, reply_id):
        """After a finished/cancelled/failed request, only a clear intent starts a new (paid) request.

        "thank you" after receiving a report must not open a new purchase flow.
        """
        has_open = ReportRequest.objects.filter(active_lock=self.customer.pk).exists()
        if has_open or wants_new_report(choice):
            return self._idle(choice, reply_id)
        self.say(self.t.IDLE_HINT)

    on_ended = on_delivered = on_report_failed = on_no_hit = _after_request

    # Customer details in the order they are asked; only the provider's required fields are asked.
    FIELD_STEPS = (  # (field, state, name of the prompt in copy.py / i18n.py)
        ("full_name", S.ASK_NAME, "ASK_NAME"),
        ("pan", S.ASK_PAN, "ASK_PAN"),
        ("dob", S.ASK_DOB, "ASK_DOB"),
        ("gender", S.ASK_EXTRA, "ASK_GENDER"),
    )

    @property
    def needed(self) -> frozenset[str]:
        return self.reports.provider.required_fields

    def _ask(self, field: str):
        for name, state, prompt in self.FIELD_STEPS:
            if name == field:
                self.go(state)
                buttons = (self.t.BTN_MALE, self.t.BTN_FEMALE) if name == "gender" else ()
                return self.say(getattr(self.t, prompt), *buttons)
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
            self.say(self.t.SAVED_DETAILS)
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
                self.start_direct_fetch(req, self.t.STILL_FETCHING)
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
            self.say(self.t.OTP_SENT.format(destination=""), self.t.BTN_RESEND)
        elif status in {ReportStatus.OTP_VERIFIED, ReportStatus.FETCHING}:
            self.go(S.FETCHING_REPORT)
            self.say(self.t.STILL_FETCHING)
        elif status == ReportStatus.GENERATED:
            if req.payment_links.filter(status=PaymentLinkStatus.PAID).exists():
                self.say(self.t.PAYMENT_RECEIVED)  # paid, delivery in progress: never ask for payment again
                return
            self.go(S.AWAITING_PAYMENT)
            self.on_awaiting_payment("", None)

    # -- details ----------------------------------------------------------------------------------------

    def on_ask_name(self, choice, reply_id):
        name = clean_name(choice)
        if not name:
            return self.say(self.t.INVALID_NAME)
        self.customer.full_name = name
        self.customer.save(update_fields=["full_name", "updated_at"])
        self.ask_next_after("full_name")

    def on_ask_pan(self, choice, reply_id):
        pan = clean_pan(choice)
        if not pan:
            return self.say(self.t.INVALID_PAN)
        self.customer.set_pan(pan)
        self.customer.save(update_fields=["pan", "pan_hash", "updated_at"])
        self.ask_next_after("pan")

    def on_ask_dob(self, choice, reply_id):
        dob = clean_dob(choice)
        if not dob:
            return self.say(self.t.INVALID_DOB)
        self.customer.dob = dob.isoformat()
        self.customer.save(update_fields=["dob", "updated_at"])
        self.ask_next_after("dob")

    def on_ask_extra(self, choice, reply_id):  # gender (asked only when the provider needs it)
        gender = {"male": "M", "m": "M", "female": "F", "f": "F"}.get((reply_id or choice).strip().lower())
        if not gender:
            return self.say(self.t.INVALID_GENDER, self.t.BTN_MALE, self.t.BTN_FEMALE)
        self.customer.gender = gender
        self.customer.save(update_fields=["gender", "updated_at"])
        self.ask_next_after("gender")

    def prompt_confirm(self, pending: dict | None = None):
        c = self.customer
        name = pending["full_name"] if pending else c.full_name
        pan = pending["pan"] if pending else c.pan
        gender = pending["gender"] if pending else c.gender
        # The customer sees their full name and PAN with only the middle two digits hidden (business
        # decision 2026-11-27); a number registered to someone else still never reveals more than that.
        lines = [f"{self.t.LABEL_NAME}: {name}", f"{self.t.LABEL_PAN}: {mask_pan_digits(pan)}"]
        if "dob" in self.needed and c.dob and not pending:
            lines.append(f"{self.t.LABEL_DOB}: {c.dob[8:10]}-{c.dob[5:7]}-****")
        if "gender" in self.needed and gender:
            label = {Gender.MALE: self.t.BTN_MALE[1], Gender.FEMALE: self.t.BTN_FEMALE[1]}.get(gender, gender)
            lines.append(f"{self.t.LABEL_GENDER}: {label}")
        self.go(S.CONFIRM_DETAILS)
        if pending:
            self.say(
                self.t.CONFIRM_PREFILL.format(details="\n".join(lines)),
                self.t.BTN_ITS_ME,
                self.t.BTN_NOT_ME,
                self.t.BTN_RESET,
            )
        else:
            self.say(
                self.t.CONFIRM_DETAILS.format(details="\n".join(lines)),
                self.t.BTN_CONFIRM,
                self.t.BTN_EDIT,
                self.t.BTN_RESET,
            )

    def run_prefill(self, mobile: str | None = None):
        """After consent: look up the details linked to the WhatsApp number (or an alternate number the
        customer sent). Nothing found -> offer another number; provider problem -> ask the customer."""
        try:
            found = self.reports.prefill(self.request.pk, mobile)
        except PrefillLimitReached:
            return self.type_details(self.t.PREFILL_LIMIT)
        except (TransientError, PermanentError, LockNotAcquired):
            return self.type_details(self.t.PREFILL_UNAVAILABLE)
        if mobile:
            return self._alternate_result(mobile, found)
        if found is None:
            return self.offer_alt_mobile(
                self.t.OWN_NUMBER_NOT_FOUND.format(mobile=self.customer.masked_phone)
            )
        save_pending(self.customer, {"full_name": found.full_name, "pan": found.pan, "gender": found.gender})
        self.show_pending(load_pending(self.customer))

    def show_pending(self, pending: dict):
        missing = [f for f in ("full_name", "pan", "gender") if f in self.needed and not pending.get(f)]
        if missing:
            # Partial match: keep what was found and let the customer fill the gap (saved on confirm).
            self.apply_pending(pending)
            clear_pending(self.customer)
            self.say(self.t.PREFILL_NOT_FOUND)
            return self._ask(missing[0])
        self.prompt_confirm(pending=pending)

    def type_details(self, message: str | None = None):
        clear_pending(self.customer)
        self.say(message or self.t.TYPE_DETAILS)
        self.ask_next_after(None)

    # -- alternate mobile number ------------------------------------------------------------------------

    def alt_tries_left(self) -> int:
        return ALT_MOBILE_MAX_TRIES - len(self.conv.context.get("alt_tried", []))

    def offer_alt_mobile(self, intro: str):
        """Ask for another number (the customer can also choose to type their details instead)."""
        if self.alt_tries_left() <= 0:
            return self.type_details(f"{intro}\n\n{self.t.ALT_NO_MORE_TRIES}")
        self.go(S.ASK_ALT_MOBILE)
        self.say(f"{intro}\n\n{self.t.ASK_ALT_MOBILE}", self.t.BTN_TYPE_DETAILS, self.t.BTN_RESET)

    def on_ask_alt_mobile(self, choice, reply_id):
        if (reply_id or choice.lower()) in {"type_details", "type my details"}:
            return self.type_details()
        mobile = clean_mobile(choice)
        if not mobile:
            return self.say(self.t.INVALID_MOBILE, self.t.BTN_TYPE_DETAILS)
        tried = list(self.conv.context.get("alt_tried", []))
        marker = blind_index(mobile, ALT_MOBILE_PURPOSE)
        if mobile == self.customer.wa_id or marker in tried:
            return self.say(self.t.ALT_ALREADY_TRIED, self.t.BTN_TYPE_DETAILS)
        self.conv.context = {**self.conv.context, "alt_tried": [*tried, marker]}
        self.run_prefill(mobile)

    def _alternate_result(self, mobile: str, found):
        masked = mask_phone(mobile)
        if found is None or not found.pan:  # without a PAN there is nothing to prove ownership with
            return self.offer_alt_mobile(self.t.ALT_NOT_FOUND.format(mobile=masked))
        pending = {"full_name": found.full_name, "pan": found.pan, "gender": found.gender}
        save_pending(self.customer, {**pending, "mobile": mobile, "verified": False})
        self.ask_alt_pan(masked)

    def ask_alt_pan(self, masked_mobile: str):
        # Nothing from the lookup is shown yet: a stranger's number must not reveal whose it is.
        self.go(S.VERIFY_ALT_PAN)
        self.say(
            self.t.ALT_VERIFY_PAN.format(mobile=masked_mobile), self.t.BTN_TYPE_DETAILS, self.t.BTN_RESET
        )

    def on_verify_alt_pan(self, choice, reply_id):
        if (reply_id or choice.lower()) in {"type_details", "type my details"}:
            return self.type_details()
        pending = load_pending(self.customer)
        if not pending or not pending.get("mobile"):  # expired (30 min): start the typed route
            return self.type_details(self.t.PREFILL_NOT_FOUND)
        pan = clean_pan(choice)
        if not pan:
            return self.say(self.t.INVALID_PAN, self.t.BTN_TYPE_DETAILS)
        if not hmac.compare_digest(pan, pending.get("pan") or ""):
            # One chance only: retrying would let someone guess a PAN against the bureau's record.
            audit.record("report.alternate_mobile_rejected", actor_type=ActorType.SYSTEM, obj=self.request)
            return self.type_details(self.t.ALT_PAN_MISMATCH.format(mobile=mask_phone(pending["mobile"])))
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
            verification = self.t.CONSENT_PREFILL_LINE.format(mobile=self.customer.masked_phone)
        elif direct:
            verification = self.t.CONSENT_DIRECT_LINE.format(mobile=self.customer.masked_phone)
        else:
            verification = self.t.CONSENT_OTP_LINE
        text = self.t.CONSENT_TEXT.format(
            verification=verification,
            price=format_rupees(settings.REPORT_PRICE_PAISE),
            retention=settings.REPORT_PDF_RETENTION_DAYS,
        )
        flavour = "prefill" if prefill else "direct" if direct else "otp"
        # The exact text differs per language, so the version names it (English keeps its old names).
        lang = "" if self.t.language == "en" else f"-{self.t.language}"
        return f"{self.t.CONSENT_VERSION}-{flavour}{lang}", text

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
                return self.start_direct_fetch(req, self.t.FETCHING_DIRECT)
            self.conv.context = {**self.conv.context, "details_confirmed": True}
            self.go(S.AWAITING_CONSENT)
            self.say(self.consent()[1], self.t.BTN_AGREE, self.t.BTN_DECLINE)
        elif answer in {"not_me", "no, not me"} or (pending and answer in {"edit", "no", "n", "2"}):
            clear_pending(self.customer)
            from_whatsapp_number = pending is not None and not pending.get("mobile")
            if from_whatsapp_number and consented and self.prefill_mode and self.alt_tries_left() > 0:
                return self.offer_alt_mobile(self.t.NOT_ME_OFFER_ALT)  # e.g. a recycled number
            self.say(self.t.PREFILL_NOT_ME)
            self.ask_next_after(None)
        elif answer in {"edit", "no", "n", "2"}:
            self.ask_next_after(None)
        elif pending:
            self.say(self.t.PLEASE_CHOOSE, self.t.BTN_ITS_ME, self.t.BTN_NOT_ME)
        else:
            self.say(self.t.PLEASE_CHOOSE, self.t.BTN_CONFIRM, self.t.BTN_EDIT)

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
            elif self.details_complete() and (
                self.conv.context.get("details_confirmed") or not self.prefill_mode
            ):
                self.start_direct_fetch(self.request, self.t.FETCHING_DIRECT)
            elif self.prefill_mode:
                self.run_prefill()
            else:
                # Settings changed mid-conversation (or details were cleared): never fetch without them.
                self.ask_next_after(None)
        elif answer in {"decline", "no", "n", "no, thanks"}:
            req = self.request
            if req is not None and req.is_open:
                self.reports.cancel(req.pk, by_user=None, reason="consent_declined")
            clear_pending(self.customer)
            self.conv.active_report_request = None
            self.go(S.ENDED)
            self.say(self.t.DECLINED)
        else:
            self.say(self.t.PLEASE_CHOOSE, self.t.BTN_AGREE, self.t.BTN_DECLINE)

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
            return self.say(self.t.OTP_RESEND_LIMIT)
        except (TransientError, LockNotAcquired):
            self.go(S.OTP_FAILED)
            return self.say(self.t.OTP_TRY_AGAIN, self.t.BTN_RETRY)
        except PermanentError as exc:
            self.reports.mark_failed(req.pk, exc.code)
            self.go(S.REPORT_FAILED)
            return self.say(self.t.REJECTED)
        self.go(S.AWAITING_OTP)
        if resend:
            self.say(self.t.OTP_RESENT, self.t.BTN_RESEND)
        else:
            self.say(self.t.OTP_SENT.format(destination=session.masked_destination), self.t.BTN_RESEND)

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
            return self.say(self.t.OTP_NOT_NUMERIC, self.t.BTN_RESEND)
        try:
            outcome = self.reports.verify_otp(req.pk, choice)
        except (TransientError, LockNotAcquired):
            return self.say(self.t.OTP_VERIFY_LATER)
        req.refresh_from_db()
        if outcome == VerifyOutcome.VERIFIED:
            self.go(S.FETCHING_REPORT)
            self.say(self.t.FETCHING)
            from apps.reports.tasks import fetch_report_task

            self.effects.append(lambda pk=str(req.pk): fetch_report_task.delay(pk))
        elif req.report_status == ReportStatus.FAILED:
            self.go(S.REPORT_FAILED)
            self.say(self.t.OTP_TOO_MANY)
        elif outcome == VerifyOutcome.EXPIRED:
            self.say(self.t.OTP_EXPIRED, self.t.BTN_RESEND)
        else:
            left = max(settings.OTP_MAX_ATTEMPTS - req.otp_attempts, 0)
            self.say(self.t.OTP_INVALID.format(left=left), self.t.BTN_RESEND)

    # -- after verification -----------------------------------------------------------------------------

    def on_fetching_report(self, choice, reply_id):
        req = self.request
        if req is not None and req.report_status not in {ReportStatus.OTP_VERIFIED, ReportStatus.FETCHING}:
            return self.resume(req) if req.is_open else self._after_request(choice, reply_id)
        self.say(self.t.STILL_FETCHING)

    def on_awaiting_payment(self, choice, reply_id):
        req = self.request
        if req is None or not req.is_open:
            return self._after_request(choice, reply_id)
        if req.payment_links.filter(status=PaymentLinkStatus.PAID).exists():
            return self.say(self.t.PAYMENT_RECEIVED)
        try:
            link = PaymentService().get_or_create_link(req.pk)
        except (TransientError, PermanentError):
            return self.say(self.t.UNAVAILABLE)
        self.say(
            self.t.PAYMENT_REMINDER.format(
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
            list_button=reply.list_button,
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
            bot.say(bot.t.UNSUPPORTED)
        elif text is None and reply_id is None:
            bot.say(bot.t.LOST_MESSAGE)
        else:
            bot.handle(text or "", reply_id, wa_message_id)
        conv.save()

        cache.set(_REPLIES_KEY + wa_message_id, [r.__dict__ for r in bot.replies], timeout=60 * 60)
        _send(customer, conv, bot.replies, wa_message_id)
    for effect in bot.effects:
        effect()
    return conv.state

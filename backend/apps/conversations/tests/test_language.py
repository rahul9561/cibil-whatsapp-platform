"""Language choice: asked first (5 languages as a list menu), then every message in that language."""

import re
import string

import pytest

from apps.conversations import copy as en
from apps.conversations.i18n import LANGUAGE_MENU, TRANSLATIONS, copy_for, parse_language
from apps.conversations.models import Conversation
from apps.conversations.models import ConversationState as S
from apps.customers.models import Customer
from apps.messaging.dev import simulate_inbound
from apps.messaging.gateway.mock import outbox
from apps.reports.models import ReportRequest, ReportStatus

pytestmark = pytest.mark.django_db
COMMANDS = ("HI", "CANCEL", "STOP", "START", "HELP", "LANGUAGE")


@pytest.fixture(autouse=True)
def ask_language(settings):
    settings.WHATSAPP_ASK_LANGUAGE = True
    settings.CREDIT_PROVIDER = "mock_no_otp"
    settings.PREFILL_BY_MOBILE = True


def say(wa, text=None, button=None):
    return simulate_inbound(wa, text=text, reply_id=button)


def customer(wa):
    return Customer.objects.by_wa_id(wa).get()


def state(wa):
    return Conversation.objects.get(customer=customer(wa)).state


# --- the flow --------------------------------------------------------------------------------------------


def test_first_message_asks_language_as_a_list_of_five():
    wa = "919800033301"
    r = say(wa, "Hi")
    assert len(r) == 1 and "Please choose your language" in r[0]["body"] and "भाषा चुनें" in r[0]["body"]
    sent = outbox()[-1]
    assert sent["kind"] == "list" and sent["button_text"] == "Language / भाषा"
    assert sent["buttons"] == ["lang_en", "lang_hi", "lang_pa", "lang_te", "lang_ta"]
    assert not ReportRequest.objects.exists()  # nothing starts before the language is known


def test_choosing_hindi_runs_the_whole_flow_in_hindi():
    wa = "919800033302"
    say(wa, "Hi")
    r = say(wa, button="lang_hi")
    assert customer(wa).language == "hi"
    body = " || ".join(x["body"] for x in r)
    assert "नमस्ते" in body and "क्या आप सहमत हैं?" in body  # welcome + consent in Hindi
    assert outbox()[-1]["buttons"] == ["agree", "decline"]  # same button ids in every language

    r = say(wa, button="agree")
    shown = " || ".join(x["body"] for x in r)
    assert "क्या यह आप हैं?" in shown and "नाम: R*** S***" in shown and "लिंग: पुरुष" in shown

    r = say(wa, button="confirm")
    assert "क्रेडिट रिपोर्ट मँगा रहे हैं" in " ".join(x["body"] for x in r)
    req = ReportRequest.objects.get()
    assert req.report_status == ReportStatus.GENERATED
    assert req.consent.consent_text_version.endswith("-prefill-hi")  # Hindi consent text is recorded
    assert "भुगतान करें" in outbox()[-1]["body"]  # payment link text in Hindi too


@pytest.mark.parametrize(
    ("typed", "code", "word"),
    [("3", "pa", "ਸਤ ਸ੍ਰੀ ਅਕਾਲ"), ("telugu", "te", "నమస్కారం"), ("தமிழ்", "ta", "வணக்கம்")],
)
def test_language_can_be_typed(typed, code, word):
    wa = f"91980003331{len(code) + len(typed) % 7}"
    say(wa, "Hi")
    r = say(wa, typed)
    assert customer(wa).language == code and word in r[0]["body"]


def test_anything_else_shows_the_menu_again():
    wa = "919800033303"
    say(wa, "Hi")
    r = say(wa, "what is this")
    assert "Please choose your language" in r[0]["body"] and customer(wa).language == ""


def test_returning_customer_is_not_asked_again():
    wa = "919800033304"
    say(wa, "Hi")
    say(wa, button="lang_ta")
    say(wa, "CANCEL")
    r = say(wa, "Hi")
    assert "வணக்கம்" in r[0]["body"] and outbox()[-1]["kind"] != "list"


def test_language_command_switches_mid_flow_and_resumes():
    wa = "919800033305"
    say(wa, "Hi")
    say(wa, button="lang_en")
    assert state(wa) == S.AWAITING_CONSENT
    r = say(wa, "LANGUAGE")
    assert "Please choose your language" in r[0]["body"]
    r = say(wa, button="lang_pa")
    body = " || ".join(x["body"] for x in r)
    assert "ਪੰਜਾਬੀ ਵਿੱਚ ਗੱਲ ਕਰਾਂਗੇ" in body and "ਕੀ ਤੁਸੀਂ ਸਹਿਮਤ ਹੋ?" in body  # consent repeated in Punjabi
    assert customer(wa).language == "pa" and state(wa) == S.AWAITING_CONSENT


def test_commands_still_work_in_any_language():
    wa = "919800033306"
    say(wa, "Hi")
    say(wa, button="lang_te")
    r = say(wa, "HELP")
    assert "కమాండ్‌లు" in r[0]["body"] and "LANGUAGE" in r[0]["body"]


def test_parse_language():
    assert parse_language(None, "Hi") is None  # a greeting, not Hindi
    assert parse_language("lang_hi", "") == "hi"
    assert parse_language("lang_xx", "") is None
    assert parse_language(None, " Hindi ") == "hi"
    assert parse_language(None, "हिंदी") == "hi"
    assert parse_language(None, "5") == "ta"
    assert parse_language(None, "hello") is None


# --- the translations themselves -------------------------------------------------------------------------


def _english_names():
    return [n for n in dir(en) if n.isupper() and isinstance(getattr(en, n), (str, tuple))]


def _placeholders(text):
    return sorted(f for _, f, _, _ in string.Formatter().parse(text) if f)


@pytest.mark.parametrize("lang", ["hi", "pa", "te", "ta"])
def test_every_message_is_translated_with_the_same_placeholders(lang):
    strings = TRANSLATIONS[lang]
    skip = {"CONSENT_VERSION", "CHOOSE_LANGUAGE", "LANGUAGE_LIST_BUTTON"}  # same in every language
    missing = [n for n in _english_names() if n not in strings and n not in skip]
    assert missing == [], f"{lang}: not translated: {missing}"
    for name, value in strings.items():
        english = getattr(en, name)
        if isinstance(english, tuple):  # button: same id, title within WhatsApp's 20 characters
            assert value[0] == english[0], (lang, name)
            assert 0 < len(value[1]) <= 20, (lang, name, value[1], len(value[1]))
        else:
            assert _placeholders(value) == _placeholders(english), (lang, name)
            for command in COMMANDS:  # commands the English text tells people to send stay in English
                if re.search(rf"\b{command}\b", english):
                    assert command in value, (lang, name, command)


def test_menu_labels_fit_and_untranslated_names_fall_back_to_english():
    assert all(len(label) <= 24 for _, label in LANGUAGE_MENU)
    assert len(en.LANGUAGE_LIST_BUTTON) <= 20
    assert copy_for("hi").CONSENT_VERSION == en.CONSENT_VERSION
    assert copy_for("xx").WELCOME == en.WELCOME and copy_for(None).WELCOME == en.WELCOME

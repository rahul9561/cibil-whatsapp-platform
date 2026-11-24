"""Every customer-facing bot message, in one place for review and translation (English only for now).

CONSENT_TEXT is a DRAFT for legal review (DPDP Act 2023, CICRA 2005, provider agreement). Changing its
wording requires a new CONSENT_VERSION: the version and a hash of the exact text are stored with each
consent record.
"""

CONSENT_VERSION = "v1-draft"
CONSENT_TEXT = (
    "Before we continue, please read and confirm:\n\n"
    "1. You authorise us to obtain YOUR credit report from an authorised credit information company "
    "through our licensed partner, only to share it with you.\n"
    "2. {verification}\n"
    "3. The report fee of {price} is payable only after your report is ready.\n"
    "4. Your details are stored encrypted and the report file is deleted after {retention} days.\n"
    "5. You can withdraw consent any time by replying CANCEL, and stop messages with STOP.\n\n"
    "Do you agree?"
)

WELCOME = "Hello! I can get your CIBIL credit report on WhatsApp. It takes about 2 minutes."
ASK_NAME = "Please type your full name exactly as on your PAN card."
ASK_PAN = "Thanks. Now please type your 10-character PAN (for example ABCDE1234F)."
ASK_DOB = "Please type your date of birth as DD-MM-YYYY (for example 15-01-1990)."
CONFIRM_DETAILS = "Please check your details:\n\n{details}\n\nIs this correct?"
SAVED_DETAILS = "Welcome back! We have your details from last time."

INVALID_NAME = "That doesn't look like a name. Please use letters only (2 to 100 characters)."
INVALID_PAN = "That isn't a valid PAN. It should look like ABCDE1234F (5 letters, 4 digits, 1 letter)."
INVALID_DOB = "Please send a valid date of birth as DD-MM-YYYY. You must be at least 18 years old."
PLEASE_CHOOSE = "Please choose one of the options below."

OTP_SENT = "We've sent an OTP to your registered mobile number {destination}. Please reply with the OTP."
OTP_RESENT = "We've sent a new OTP. Please reply with it."
OTP_INVALID = "That OTP is not correct. You have {left} attempt(s) left."
OTP_EXPIRED = "That OTP has expired. Tap Resend OTP to get a new one."
OTP_TOO_MANY = "Too many incorrect attempts, so this request was closed. Send HI to start again."
OTP_RESEND_LIMIT = "We can't send more OTPs for this request. Reply CANCEL and then HI to start again."
OTP_TRY_AGAIN = "We couldn't reach the credit bureau just now. Please tap Try again in a minute."
OTP_VERIFY_LATER = "We couldn't verify the OTP just now. Please send it again in a minute."
OTP_NOT_NUMERIC = "Please reply with the numeric OTP only, or tap Resend OTP."

FETCHING = "Verified! We're preparing your credit report. This usually takes under a minute."
STILL_FETCHING = "Your report is still being prepared. We'll message you as soon as it's ready."
NO_HIT = (
    "The credit bureau has no credit history for these details, so there is no report to share. "
    "You have not been charged. Send HI if you'd like to try again with different details."
)
REPORT_FAILED = (
    "Sorry, we couldn't get your report right now. You have not been charged. "
    "Please send HI to try again later."
)
REJECTED = (
    "Sorry, the credit bureau couldn't process these details. You have not been charged. "
    "Please check your details and send HI to try again."
)

PAYMENT_REMINDER = "Your report {ref} is ready. Pay {amount} here to receive it: {url}"
PAYMENT_RECEIVED = "We've received your payment. Your report is on its way."
DELIVERED_THANKS = "Your report has been sent. Send HI any time if you need another one."

CANCELLED = "Your request has been cancelled. Send HI whenever you'd like to start again."
CANNOT_CANCEL = "Your report is already being prepared or paid for, so it can't be cancelled here."
DECLINED = "No problem, nothing has been requested. Send HI if you change your mind."
OPTED_OUT = "You won't receive reminders from us any more. Reply START to turn them back on."
OPTED_IN = "Reminders are back on."
UNSUPPORTED = "Sorry, I can only read text messages. Please type your reply."
LOST_MESSAGE = "Sorry, we couldn't read your last message. Please send it again."
UNAVAILABLE = "Sorry, this service is not available right now. Please try again later."
HELP = (
    "Commands:\nHI - start or continue a request\nCANCEL - cancel your current request\n"
    "STOP - stop reminders\nSTART - turn reminders back on\nHELP - show this message"
)

# Button ids are our own fixed strings (never personal data); titles max 20 characters.
BTN_CONFIRM = ("confirm", "Yes, correct")
BTN_EDIT = ("edit", "Edit details")
BTN_AGREE = ("agree", "I agree")
BTN_DECLINE = ("decline", "No, thanks")
BTN_RESEND = ("resend", "Resend OTP")
BTN_RETRY = ("retry", "Try again")
BTN_USE_SAVED = ("confirm", "Use these details")

REQUEST_TIMED_OUT = (
    "Your request timed out because we didn't hear back. Send HI whenever you'd like to start again."
)
REPORT_EXPIRED = (
    "Your report {ref} has expired because it wasn't paid for in time. You have not been charged. "
    "Send HI if you'd like a fresh report."
)

IDLE_HINT = "Thanks for your message! Send HI whenever you'd like a new credit report, or HELP for options."

# Line 2 of the consent text depends on how the provider verifies the customer.
CONSENT_OTP_LINE = (
    "The credit bureau will send a one-time password (OTP) to the mobile number registered with it."
)
CONSENT_DIRECT_LINE = (
    "Your report will be requested using the details above and this WhatsApp number ({mobile}). "
    "Only request a report for your own PAN."
)
ASK_GENDER = "Please choose your gender as recorded with the credit bureau."
INVALID_GENDER = "Please choose one of the options below."
BTN_MALE = ("male", "Male")
BTN_FEMALE = ("female", "Female")
FETCHING_DIRECT = "Thank you! We're fetching your credit report now. This usually takes under a minute."
PAN_BLOCKED = (
    "For your security we couldn't process this request: this PAN or WhatsApp number was recently used for "
    "another report. You have not been charged. Please contact our support team."
)

# Prefill-by-mobile (details looked up from the customer's WhatsApp number after consent).
CONSENT_PREFILL_LINE = (
    "We will look up the details the credit bureau links to this WhatsApp number ({mobile}) - your name, PAN "
    "and gender - and use them to request your report. Only continue if this number and PAN are your own."
)
CONFIRM_PREFILL = "We found these details linked to your number:\n\n{details}\n\nIs this you?"
PREFILL_NOT_FOUND = "We couldn't find your details automatically, so please type them in."
PREFILL_UNAVAILABLE = "We couldn't look up your details right now, so please type them in."
PREFILL_NOT_ME = "No problem, those details have been discarded. Please type your own details."
BTN_ITS_ME = ("confirm", "Yes, that's me")
BTN_NOT_ME = ("not_me", "No, not me")
PREFILL_LIMIT = "You've reached today's limit for automatic look-ups, so please type your details."

# Alternate mobile number: offered when nothing is found for the WhatsApp number, or the details found
# belong to someone else. Details found for another number are only used after the customer types the
# matching PAN, so nobody can see who a number belongs to.
OWN_NUMBER_NOT_FOUND = (
    "We couldn't find any credit details linked to this WhatsApp number ({mobile}).\n\n"
    "Is your credit history linked to a different mobile number, for example the one registered with "
    "your bank or loan?"
)
NOT_ME_OFFER_ALT = (
    "No problem, those details have been discarded.\n\n"
    "Is your credit history linked to a different mobile number?"
)
ASK_ALT_MOBILE = (
    "Please send the other 10-digit mobile number.\n\n"
    "Only send a number that is your own. By sending it, you allow us to use it to look up your details "
    "and request your credit report."
)
INVALID_MOBILE = (
    "That doesn't look like a valid Indian mobile number. Please send 10 digits, e.g. 98765 43210."
)
ALT_ALREADY_TRIED = "We've already checked that number. Please send a different one, or type your details."
ALT_NOT_FOUND = "We couldn't find any credit details linked to {mobile} either."
ALT_NO_MORE_TRIES = "Let's continue with your details instead. Please type them in."
ALT_VERIFY_PAN = (
    "We found details linked to {mobile}. To confirm they're yours, please type your PAN "
    "(10 characters, e.g. ABCDE1234F)."
)
ALT_PAN_MISMATCH = (
    "That PAN doesn't match the details linked to {mobile}, so we haven't used them. "
    "Please type your own details."
)
TYPE_DETAILS = "Sure, please type your details."
BTN_TYPE_DETAILS = ("type_details", "Type my details")

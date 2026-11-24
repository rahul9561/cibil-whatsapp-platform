"""WhatsApp message templates: the single source of truth for names, wording and parameters.

The same definitions feed three places, so they cannot drift apart:
  * settings.WHATSAPP_TEMPLATES (logical key -> approved template name, overridable by env),
  * MessagingService, which validates parameters before anything is sent,
  * scripts/submit_whatsapp_templates.py, which submits the wording to Meta for approval.

Pure Python (no Django imports) so settings and the submission script can load it.
Changing a body after approval needs a NEW template name (e.g. _v2): Meta does not let an approved
template's meaning change silently, and old messages in flight still reference the old name.
"""

import re
from dataclasses import dataclass, field

PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")
# Meta rejects body parameters containing newlines or tabs, or more than four consecutive spaces.
_BAD_PARAM = re.compile(r"[\n\r\t]| {5,}")
MAX_PARAM_LENGTH = 1024


@dataclass(frozen=True)
class TemplateDef:
    key: str  # logical key used in code
    default_name: str  # name submitted to Meta
    env_var: str  # env override for the name
    body: str
    body_examples: tuple[str, ...]  # one example per {{n}}, in order (required by Meta for review)
    footer: str = ""
    header_format: str = ""  # "" or "DOCUMENT"
    header_example_filename: str = ""
    category: str = "UTILITY"
    language: str = "en"
    used_by: tuple[str, ...] = field(default_factory=tuple)  # where the code sends it (for reviewers)

    @property
    def param_count(self) -> int:
        return len(PLACEHOLDER.findall(self.body))

    @property
    def has_document_header(self) -> bool:
        return self.header_format == "DOCUMENT"


TEMPLATES: dict[str, TemplateDef] = {
    t.key: t
    for t in (
        TemplateDef(
            key="payment_link",
            default_name="payment_link_v1",
            env_var="WA_TEMPLATE_PAYMENT_LINK",
            body=(
                "Your credit report {{1}} is ready. Please pay {{2}} using this secure Razorpay link to "
                "receive your report PDF here on WhatsApp: {{3}}\n\n"
                "Please pay before the link expires. Reply HELP if you need assistance."
            ),
            body_examples=("CR-9EW5HDYN", "₹99", "https://rzp.io/i/AbC12xYz"),
            footer="Secure payment by Razorpay",
            used_by=(
                "payments.services.PaymentService.send_link (first send)",
                "customers.api messages (staff, template_key=payment_link)",
                "reports.api resend link (staff)",
            ),
        ),
        TemplateDef(
            key="payment_reminder",
            default_name="payment_reminder_v1",
            env_var="WA_TEMPLATE_PAYMENT_REMINDER",
            body=(
                "Reminder: your credit report {{1}} is still waiting for payment. Pay {{2}} here to "
                "receive the PDF: {{3}}\n\n"
                "If you no longer need the report, you can ignore this message."
            ),
            body_examples=("CR-9EW5HDYN", "₹99", "https://rzp.io/i/AbC12xYz"),
            footer="Reply STOP to stop reminders",
            used_by=(
                "followups.tasks.send_reminder (scheduled reminders)",
                "customers.api messages (staff, template_key=payment_reminder)",
            ),
        ),
        TemplateDef(
            key="report_delivery",
            default_name="report_delivery_v1",
            env_var="WA_TEMPLATE_REPORT_DELIVERY",
            header_format="DOCUMENT",
            header_example_filename="credit-report-CR-9EW5HDYN.pdf",
            body=(
                "Thank you for your payment. Your credit report {{1}} is attached as a PDF.\n\n"
                "Please keep it safe, as it contains your personal financial information."
            ),
            body_examples=("CR-9EW5HDYN",),
            footer="Reply HELP for assistance",
            used_by=("reports.services.ReportService.deliver (via MessagingService.send_document)",),
        ),
        TemplateDef(
            key="report_expired",
            default_name="report_expired_v1",
            env_var="WA_TEMPLATE_REPORT_EXPIRED",
            body=(
                "Your credit report {{1}} has expired because it was not paid for in time. "
                "You have not been charged.\n\n"
                "Reply HI whenever you would like a new report."
            ),
            body_examples=("CR-9EW5HDYN",),
            footer="Reply HELP for assistance",
            used_by=("reports.maintenance.expire_stale_requests (unpaid after validity)",),
        ),
    )
}


class TemplateParamError(ValueError):
    pass


def check_definition(t: TemplateDef) -> None:
    """Raise if a definition would be rejected by Meta or mis-sent by our code."""
    numbers = [int(n) for n in PLACEHOLDER.findall(t.body)]
    if numbers != list(range(1, len(numbers) + 1)):
        raise TemplateParamError(
            f"{t.key}: placeholders must be {{{{1}}}}..{{{{n}}}} in order, got {numbers}"
        )
    if re.match(r"\s*\{\{\d+\}\}", t.body) or re.search(r"\{\{\d+\}\}\s*$", t.body):
        raise TemplateParamError(f"{t.key}: body may not start or end with a variable")
    if len(t.body_examples) != t.param_count:
        raise TemplateParamError(f"{t.key}: {t.param_count} placeholders but {len(t.body_examples)} examples")
    if not re.fullmatch(r"[a-z0-9_]{1,512}", t.default_name):
        raise TemplateParamError(f"{t.key}: template names are lowercase letters, digits and _")
    if t.header_format not in ("", "DOCUMENT"):
        raise TemplateParamError(f"{t.key}: unsupported header {t.header_format}")
    if t.has_document_header and not t.header_example_filename.endswith(".pdf"):
        raise TemplateParamError(f"{t.key}: document header needs a .pdf example filename")
    if len(t.footer) > 60:
        raise TemplateParamError(f"{t.key}: footer longer than 60 characters")
    if len(t.body) > 1024:
        raise TemplateParamError(f"{t.key}: body longer than 1024 characters")
    validate_params(t.key, t.body_examples)


def validate_params(key: str, params) -> list[str]:
    """Return params as strings, or raise TemplateParamError before anything reaches WhatsApp."""
    t = TEMPLATES.get(key)
    if t is None:
        raise TemplateParamError(f"unknown template key {key!r}")
    values = [str(p) for p in params]
    if len(values) != t.param_count:
        raise TemplateParamError(f"{key}: expected {t.param_count} parameters, got {len(values)}")
    for i, v in enumerate(values, start=1):
        if not v.strip():
            raise TemplateParamError(f"{key}: parameter {{{{{i}}}}} is empty")
        if _BAD_PARAM.search(v):
            raise TemplateParamError(f"{key}: parameter {{{{{i}}}}} has a newline, tab or 5+ spaces")
        if len(v) > MAX_PARAM_LENGTH:
            raise TemplateParamError(f"{key}: parameter {{{{{i}}}}} is too long")
    return values


def default_names(env=None) -> dict[str, str]:
    """Logical key -> template name, honouring the env override for each one."""
    return {
        k: (env(t.env_var, default=t.default_name) if env else t.default_name) for k, t in TEMPLATES.items()
    }


for _t in TEMPLATES.values():  # fail at import, not at 2 a.m. in a Celery task
    check_definition(_t)

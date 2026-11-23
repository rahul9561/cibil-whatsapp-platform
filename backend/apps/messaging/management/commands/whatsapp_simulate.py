"""Chat with the bot locally (mock adapters only).

    python manage.py whatsapp_simulate 919812345678 "hi"
    python manage.py whatsapp_simulate 919812345678 --button confirm
    python manage.py whatsapp_simulate 919812345678 --pay      # mock: pay the open payment link

Test PANs and OTPs for the mock provider are listed in apps/reports/providers/mock.py (OTP 123456 works).
"""

from django.core.management.base import BaseCommand, CommandError

from apps.messaging.dev import simulate_inbound


class Command(BaseCommand):
    help = "Send a simulated customer WhatsApp message through the bot and print its replies."

    def add_arguments(self, parser):
        parser.add_argument("wa_id", help="Customer number with country code, digits only")
        parser.add_argument("text", nargs="?", default=None)
        parser.add_argument("--button", help="Button id to press (e.g. confirm, agree, resend)")
        parser.add_argument("--pay", action="store_true", help="Pay the customer's open payment link (mock)")

    def handle(self, *args, wa_id, text, button, pay, **options):
        try:
            if pay:
                from apps.customers.models import Customer
                from apps.payments.gateway.mock import simulate_payment
                from apps.payments.models import PaymentLink
                from apps.payments.tasks import confirm_payment_task

                customer = Customer.objects.by_wa_id(wa_id).get()
                link = PaymentLink.objects.filter(report_request__customer=customer, status="active").latest(
                    "created_at"
                )
                simulate_payment(link.razorpay_link_id)
                self.stdout.write(
                    f"paid {link.reference_id}: {confirm_payment_task.delay(link.razorpay_link_id).get()}"
                )
                return
            replies = simulate_inbound(wa_id, text=text, reply_id=button)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        for r in replies:
            body = r.get("body") or f"[{r['kind']}] {r.get('template') or r.get('filename') or ''}"
            buttons = f"  [buttons: {', '.join(r['buttons'])}]" if r.get("buttons") else ""
            self.stdout.write(f"BOT: {body}{buttons}\n")

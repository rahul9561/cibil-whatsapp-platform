"""Local development only: one staff account per role, plus optional demo data.

    python manage.py seed_dev_staff               # staff accounts
    python manage.py seed_dev_staff --demo-data   # + a few customers/requests through the real (mock) flow

All accounts share DEV_PASSWORD below. Refuses to run unless DEBUG is on and adapters are mocks.
Super Admin, Admin and Finance must enrol 2FA at first sign-in (as in production).
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Role

DEV_PASSWORD = "dev-only-Passw0rd-123"  # noqa: S105  (local development seed, never used in production)
DEV_STAFF = [
    ("dev-superadmin@example.com", Role.SUPER_ADMIN),
    ("dev-admin@example.com", Role.ADMIN),
    ("dev-agent@example.com", Role.AGENT),
    ("dev-finance@example.com", Role.FINANCE),
    ("dev-auditor@example.com", Role.AUDITOR),
]


class Command(BaseCommand):
    help = "Create development staff accounts (DEBUG only)."

    def add_arguments(self, parser):
        parser.add_argument("--demo-data", action="store_true")

    def handle(self, *args, demo_data, **options):
        if not settings.DEBUG or settings.CREDIT_PROVIDER != "mock" or settings.PAYMENT_GATEWAY != "mock":
            raise CommandError("seed_dev_staff only runs with DEBUG on and mock adapters.")
        User = get_user_model()
        for email, role in DEV_STAFF:
            user, created = User.objects.get_or_create(email=email, defaults={"role": role})
            user.role = role
            user.is_staff = user.is_superuser = role == Role.SUPER_ADMIN
            user.set_password(DEV_PASSWORD)
            user.save()
            self.stdout.write(f"{'created' if created else 'reset  '} {email} ({role})")
        self.stdout.write("Password: see DEV_PASSWORD in apps/accounts/management/commands/seed_dev_staff.py")
        if demo_data:
            self._demo()

    def _demo(self):
        import secrets

        from apps.core.testing.api import make_request

        def number():  # random, so repeated seeding never collides with earlier demo customers
            return f"9198{secrets.randbelow(10**7):07d}1"

        agent = get_user_model().objects.get(email="dev-agent@example.com")
        make_request(paid=True, wa_id=number())
        make_request(assigned_to=agent, wa_id=number())
        make_request(wa_id=number())
        make_request(pan="NOHIT0000N", wa_id=number())
        make_request(generated=False, wa_id=number())
        self.stdout.write("demo data: 1 paid, 2 unpaid (1 assigned to dev-agent), 1 no-hit, 1 draft")

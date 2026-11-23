from django.core.management.base import BaseCommand, CommandError

from apps.audit.service import verify_chain


class Command(BaseCommand):
    help = "Verify the audit log hash chain. Exits non-zero if tampering is detected."

    def handle(self, *args, **options):
        ok, bad_id, checked = verify_chain()
        if not ok:
            raise CommandError(f"Audit chain BROKEN at entry id={bad_id} (after {checked} valid entries).")
        self.stdout.write(self.style.SUCCESS(f"Audit chain OK ({checked} entries)."))

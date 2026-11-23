"""Re-encrypt every EncryptedTextField value under the newest key.

Rotation procedure:
  1. Generate a key:
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. Prepend it to FIELD_ENCRYPTION_KEYS (new first, old keys kept) and restart all processes.
  3. Run:  python manage.py rotate_encryption_keys
  4. Once it reports success, remove the old key(s) and restart again.
"""

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.fields import EncryptedTextField


def encrypted_fields():
    for model in apps.get_models():
        names = [f.name for f in model._meta.concrete_fields if isinstance(f, EncryptedTextField)]
        if names:
            yield model, names


class Command(BaseCommand):
    help = "Re-encrypt all encrypted fields with the first key in FIELD_ENCRYPTION_KEYS."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--dry-run", action="store_true", help="Count rows without writing.")

    def handle(self, *args, batch_size, dry_run, **options):
        total = 0
        for model, names in encrypted_fields():
            label = model._meta.label
            # Reading through the ORM decrypts with any configured key; .update() re-encrypts with the newest.
            rows = model._base_manager.values_list("pk", *names).order_by("pk")
            count = 0
            batch = []
            for row in rows.iterator(chunk_size=batch_size):
                batch.append(row)
                if len(batch) >= batch_size:
                    count += self._write(model, names, batch, dry_run)
                    batch = []
            if batch:
                count += self._write(model, names, batch, dry_run)
            total += count
            self.stdout.write(f"{label}: {count} rows ({', '.join(names)})")
        verb = "would re-encrypt" if dry_run else "re-encrypted"
        self.stdout.write(self.style.SUCCESS(f"Done: {verb} {total} rows."))

    @staticmethod
    def _write(model, names, batch, dry_run) -> int:
        if dry_run:
            return len(batch)
        with transaction.atomic():
            for pk, *values in batch:
                model._base_manager.filter(pk=pk).update(**dict(zip(names, values, strict=True)))
        return len(batch)

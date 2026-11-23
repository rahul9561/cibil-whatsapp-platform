from django.core.exceptions import FieldError
from django.db import models

from .crypto import decrypt_str, encrypt_str


class EncryptedTextField(models.TextField):
    """Text stored encrypted at rest. Values are decrypted transparently when loaded.

    Ciphertext is non-deterministic, so the only supported lookup is `isnull`. For equality
    searches store a blind index alongside (see `apps.core.crypto.blind_index`).
    """

    description = "Encrypted text"

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        return decrypt_str(value)

    def to_python(self, value):
        return value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return encrypt_str(str(value))

    def get_lookup(self, lookup_name):
        if lookup_name != "isnull":
            raise FieldError(
                f"'{lookup_name}' lookups are not supported on encrypted field '{self.name}'; "
                "use its blind index instead."
            )
        return super().get_lookup(lookup_name)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, "apps.core.fields.EncryptedTextField", args, kwargs

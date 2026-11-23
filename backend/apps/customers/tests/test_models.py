import pytest
from django.db import IntegrityError, connection, transaction

from apps.core.crypto import decrypt_str
from apps.core.testing.factories import ConsentFactory, CustomerFactory
from apps.customers.models import Customer

pytestmark = pytest.mark.django_db


def _raw(customer, column):
    with connection.cursor() as cur:
        cur.execute(f"SELECT {column} FROM customers_customer WHERE id = %s", [customer.pk.hex])  # noqa: S608
        return cur.fetchone()[0]


def test_pii_is_encrypted_at_rest_and_lookups_use_blind_index():
    c = CustomerFactory(wa_id="+91 98765 43210", pan="abcde1234f", full_name="Rahul Sharma")
    for column, plain in [("wa_id", "919876543210"), ("pan", "ABCDE1234F"), ("full_name", "Rahul Sharma")]:
        stored = _raw(c, column)
        assert plain not in stored
        assert decrypt_str(stored) == plain
    assert Customer.objects.by_wa_id("9876543210").get() == c
    assert Customer.objects.by_pan("ABCDE1234F").get() == c
    assert not Customer.objects.by_pan("ZZZZZ9999Z").exists()


def test_one_customer_per_whatsapp_number():
    CustomerFactory(wa_id="919876543210")
    with pytest.raises(IntegrityError), transaction.atomic():
        CustomerFactory(wa_id="+91 9876543210")  # same number, different formatting


def test_masks():
    c = CustomerFactory(wa_id="919876543210", pan="ABCDE1234F", full_name="Rahul Kumar Sharma")
    assert c.masked_phone == "+91 98******10"
    assert c.masked_pan == "AB******4F"
    assert c.masked_name == "R*** K*** S***"
    assert "9876543210" not in str(c)


def test_consent_is_immutable_except_withdrawal():
    consent = ConsentFactory()
    consent.purpose = "something_else"
    with pytest.raises(ValueError, match="immutable"):
        consent.save()
    with pytest.raises(ValueError, match="immutable"):
        consent.save(update_fields=["purpose"])
    consent.refresh_from_db()
    consent.withdraw()
    consent.refresh_from_db()
    assert consent.withdrawn_at is not None
    assert consent.purpose == "credit_report_self"

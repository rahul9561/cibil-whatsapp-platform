"""factory_boy factories shared by every app's tests. All data is synthetic."""

import hashlib

import factory
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.accounts.models import Role
from apps.customers.models import ConsentRecord, Customer
from apps.followups.models import FollowUp
from apps.payments.models import Payment, PaymentLink, PaymentStatus
from apps.reports.models import ReportRequest


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()

    email = factory.Sequence(lambda n: f"staff{n}@example.com")
    role = Role.AGENT
    password = factory.django.Password("test-password-123")


class CustomerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Customer

    full_name = factory.Faker("name", locale="en_IN")
    dob = "1990-01-15"

    @factory.lazy_attribute_sequence
    def wa_id(self, n):  # set through set_wa_id below so the blind index is filled
        # Always ends in 1: the WhatsApp mock treats numbers ending in 0000 as invalid recipients.
        return f"919{n:06d}1"

    wa_id_hash = ""

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        wa_id = kwargs.pop("wa_id")
        pan = kwargs.pop("pan", None)
        kwargs.pop("wa_id_hash", None)
        obj = model_class(*args, **kwargs)
        obj.set_wa_id(wa_id)
        if pan:
            obj.set_pan(pan)
        obj.save()
        return obj


CONSENT_TEXT = "I consent to fetching my credit report for the purpose of sharing it with me."


class ConsentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ConsentRecord

    customer = factory.SubFactory(CustomerFactory)
    purpose = "credit_report_self"
    consent_text_version = "v1"
    consent_text_sha256 = hashlib.sha256(CONSENT_TEXT.encode()).hexdigest()


class ReportRequestFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ReportRequest

    customer = factory.SubFactory(CustomerFactory)
    price_paise = 9900


class PaymentLinkFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PaymentLink

    report_request = factory.SubFactory(ReportRequestFactory)
    attempt = 1
    reference_id = factory.LazyAttribute(lambda o: f"{o.report_request.public_ref}-{o.attempt}")
    amount_paise = factory.LazyAttribute(lambda o: o.report_request.price_paise)


class PaymentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Payment

    payment_link = factory.SubFactory(PaymentLinkFactory)
    razorpay_payment_id = factory.Sequence(lambda n: f"pay_TEST{n:010d}")
    amount_paise = factory.LazyAttribute(lambda o: o.payment_link.amount_paise)
    status = PaymentStatus.CAPTURED
    captured_at = factory.LazyFunction(timezone.now)


class FollowUpFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FollowUp

    report_request = factory.SubFactory(ReportRequestFactory)

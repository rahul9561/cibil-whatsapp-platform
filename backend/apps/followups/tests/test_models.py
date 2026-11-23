import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.testing.factories import FollowUpFactory, ReportRequestFactory, UserFactory
from apps.followups.models import FollowUpNote, FollowUpStatus

pytestmark = pytest.mark.django_db


def test_one_followup_per_report_request():
    req = ReportRequestFactory()
    FollowUpFactory(report_request=req)
    with pytest.raises(IntegrityError), transaction.atomic():
        FollowUpFactory(report_request=req)


def test_closed_at_must_match_status():
    fu = FollowUpFactory()
    assert fu.is_open
    fu.status = FollowUpStatus.CLOSED_PAID
    with pytest.raises(IntegrityError), transaction.atomic():
        fu.save()  # closed without closed_at
    fu.closed_at = timezone.now()
    fu.save()
    assert not fu.is_open
    fu.status = FollowUpStatus.OPEN
    with pytest.raises(IntegrityError), transaction.atomic():
        fu.save()  # open with closed_at still set


def test_notes_are_immutable():
    note = FollowUpNote.objects.create(
        follow_up=FollowUpFactory(), author=UserFactory(), body="Called, no answer"
    )
    note.body = "rewritten history"
    with pytest.raises(ValueError):
        note.save()
    with pytest.raises(ValueError):
        note.delete()

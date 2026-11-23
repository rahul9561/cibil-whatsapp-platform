import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.core.tasks import ping


def test_celery_ping_task_runs_eagerly():
    assert ping.delay().get(timeout=5) == "pong"


def test_celery_declares_all_queues():
    from config.celery import app

    names = {q.name for q in app.conf.task_queues}
    assert names == {"default", "webhooks", "provider", "messaging", "payments", "reminders"}


@pytest.mark.django_db
def test_custom_user_uses_email_login():
    user = get_user_model().objects.create_user(email="Staff@Example.com", password="x" * 12)
    assert user.email == "staff@example.com"
    assert user.check_password("x" * 12)
    assert get_user_model().USERNAME_FIELD == "email"


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)

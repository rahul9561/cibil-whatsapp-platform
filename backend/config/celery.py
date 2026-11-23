import os

from celery import Celery
from kombu import Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

# Separate queues so a slow provider cannot block payment processing (ARCHITECTURE §6.7).
QUEUES = ("default", "webhooks", "provider", "messaging", "payments", "reminders")

app = Celery("cibil")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.task_queues = [Queue(name) for name in QUEUES]
app.autodiscover_tasks()

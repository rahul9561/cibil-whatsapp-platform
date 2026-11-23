from celery import shared_task


@shared_task(name="apps.core.tasks.ping")
def ping() -> str:
    """Smoke-test task used to confirm the worker is consuming from the broker."""
    return "pong"

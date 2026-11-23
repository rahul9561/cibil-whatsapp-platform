import logging

from celery import shared_task

from .integrations import LockNotAcquired, TransientError, backoff_seconds

logger = logging.getLogger(__name__)


@shared_task(name="apps.core.tasks.ping")
def ping() -> str:
    """Smoke-test task used to confirm the worker is consuming from the broker."""
    return "pong"


def run_with_retries(task, func, *, on_give_up=None):
    """Run `func`; retry the Celery task on TransientError with jittered exponential backoff.

    After `task.max_retries` the optional `on_give_up(exc)` runs (e.g. mark the request FAILED) and the task
    ends without raising, so failures land in a known business state instead of a dead queue.
    A busy lock (another worker on the same object) retries quickly without counting as a failure.
    """
    try:
        return func()
    except LockNotAcquired as exc:
        raise task.retry(exc=exc, countdown=5, max_retries=task.max_retries + 20) from exc
    except TransientError as exc:
        if task.request.retries >= task.max_retries:
            logger.error("giving up after %s retries: %s", task.request.retries, exc.code)
            if on_give_up:
                on_give_up(exc)
            return None
        raise task.retry(exc=exc, countdown=backoff_seconds(task.request.retries)) from exc

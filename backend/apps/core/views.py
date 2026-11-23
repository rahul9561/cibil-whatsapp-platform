"""Liveness and readiness probes. Unauthenticated, return no internal detail beyond component status."""

import logging

import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@never_cache
@require_GET
def healthz(request):
    """Process is up. Does not touch dependencies."""
    return JsonResponse({"status": "ok"})


def _check_db() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except Exception:
        logger.exception("readiness: database check failed")
        return False


def _check_redis() -> bool:
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        return bool(client.ping())
    except Exception:
        logger.exception("readiness: redis check failed")
        return False


@never_cache
@require_GET
def readyz(request):
    """Dependencies reachable. 503 if any check fails."""
    checks = {"database": _check_db(), "redis": _check_redis()}
    ok = all(checks.values())
    body = {
        "status": "ok" if ok else "unavailable",
        "checks": {name: "ok" if passed else "fail" for name, passed in checks.items()},
    }
    return JsonResponse(body, status=200 if ok else 503)

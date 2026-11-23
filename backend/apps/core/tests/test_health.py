from unittest import mock

import pytest
from django.test import Client


@pytest.fixture
def client():
    return Client()


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp["Cache-Control"].startswith("max-age=0")


def test_healthz_rejects_post(client):
    assert client.post("/healthz").status_code == 405


@pytest.mark.django_db
def test_readyz_ok_when_dependencies_up(client):
    with mock.patch("apps.core.views.redis.Redis.from_url") as from_url:
        from_url.return_value.ping.return_value = True
        resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"] == {"database": "ok", "redis": "ok"}


@pytest.mark.django_db
def test_readyz_503_when_redis_down(client):
    with mock.patch("apps.core.views.redis.Redis.from_url", side_effect=ConnectionError("down")):
        resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["redis"] == "fail"
    # Error details must not leak to the unauthenticated caller.
    assert "down" not in resp.content.decode()


def test_request_id_generated_and_returned(client):
    resp = client.get("/healthz")
    assert len(resp["X-Request-ID"]) == 32


def test_safe_upstream_request_id_is_kept(client):
    resp = client.get("/healthz", HTTP_X_REQUEST_ID="abc123-upstream-id")
    assert resp["X-Request-ID"] == "abc123-upstream-id"


def test_unsafe_upstream_request_id_is_replaced(client):
    resp = client.get("/healthz", HTTP_X_REQUEST_ID="bad id\nwith newline")
    assert resp["X-Request-ID"] != "bad id\nwith newline"
    assert len(resp["X-Request-ID"]) == 32

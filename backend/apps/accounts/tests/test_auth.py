import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import Role
from apps.audit.models import AuditLog

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.accounts.tests.urls")]

PASSWORD = "correct-horse-battery-staple"
LOGIN = "/api/v1/auth/login"


def make_user(email="agent@example.com", role=Role.AGENT, **kw):
    return get_user_model().objects.create_user(email=email, password=PASSWORD, role=role, **kw)


def token_for(device):
    return str(totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)).zfill(
        device.digits
    )


def login(client, email, password=PASSWORD):
    return client.post(LOGIN, {"email": email, "password": password}, content_type="application/json")


@pytest.fixture
def client():
    return Client()


# --- password login -----------------------------------------------------------------------------


def test_agent_without_2fa_is_fully_authenticated(client):
    make_user()
    resp = login(client, "AGENT@example.com")  # email is case-insensitive
    assert resp.status_code == 200
    body = resp.json()
    assert body["fully_authenticated"] is True
    assert body["user"]["role"] == "agent"
    assert "followup.manage" in body["user"]["capabilities"]
    assert "staff.manage" not in body["user"]["capabilities"]
    assert client.get("/protected").status_code == 200
    assert AuditLog.objects.filter(action="auth.login").count() == 1


def test_wrong_password_and_unknown_user_give_same_error(client):
    make_user()
    bad_pw = login(client, "agent@example.com", "wrong-password")
    unknown = login(client, "nobody@example.com", "wrong-password")
    assert bad_pw.status_code == unknown.status_code == 400
    assert bad_pw.json() == unknown.json() == {"detail": "Invalid email or password."}
    assert client.get("/protected").status_code == 403
    assert AuditLog.objects.filter(action="auth.login_failed").count() == 2


def test_inactive_user_cannot_log_in(client):
    make_user(is_active=False)
    assert login(client, "agent@example.com").status_code == 400


def test_lockout_after_repeated_failures_even_with_right_password(client, settings):
    settings.LOGIN_LOCKOUT_THRESHOLD = 3
    make_user()
    for _ in range(3):
        assert login(client, "agent@example.com", "nope").status_code == 400
    resp = login(client, "agent@example.com")
    assert resp.status_code == 429
    assert AuditLog.objects.filter(action="auth.login_locked").exists()


def test_successful_login_resets_failure_count(client, settings):
    settings.LOGIN_LOCKOUT_THRESHOLD = 3
    make_user()
    login(client, "agent@example.com", "nope")
    login(client, "agent@example.com", "nope")
    assert login(client, "agent@example.com").status_code == 200
    login(client, "agent@example.com", "nope")
    login(client, "agent@example.com", "nope")
    assert login(client, "agent@example.com").status_code == 200


def test_login_is_ip_throttled(client):
    for _ in range(10):
        login(client, f"x{_}@example.com", "nope")
    assert login(client, "y@example.com", "nope").status_code == 429


def test_login_requires_csrf_token():
    make_user()
    client = Client(enforce_csrf_checks=True)
    assert login(client, "agent@example.com").status_code == 403
    client.get("/api/v1/auth/csrf")
    token = client.cookies["csrftoken"].value
    resp = client.post(
        LOGIN,
        {"email": "agent@example.com", "password": PASSWORD},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert resp.status_code == 200


def test_session_key_rotates_on_login(client):
    make_user()
    client.get("/api/v1/auth/csrf")
    client.session.save()
    before = client.session.session_key
    login(client, "agent@example.com")
    assert client.session.session_key != before


def test_logout(client):
    make_user()
    login(client, "agent@example.com")
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/protected").status_code == 403
    assert client.get("/api/v1/auth/me").status_code == 403


# --- 2FA ----------------------------------------------------------------------------------------


def test_admin_must_enrol_2fa_before_access(client):
    make_user("admin@example.com", Role.ADMIN)
    body = login(client, "admin@example.com").json()
    assert body["fully_authenticated"] is False
    assert body["two_factor"] == {"required": True, "enrolled": False, "verified": False}
    assert client.get("/protected").status_code == 403

    setup = client.post("/api/v1/auth/2fa/setup")
    assert setup.status_code == 200
    assert setup.json()["otpauth_url"].startswith("otpauth://totp/")
    device = TOTPDevice.objects.get(user__email="admin@example.com")
    assert device.confirmed is False

    assert (
        client.post(
            "/api/v1/auth/2fa/confirm", {"token": "000000"}, content_type="application/json"
        ).status_code
        == 400
    )
    device.refresh_from_db()
    assert device.verify_is_allowed()[0] is False  # enrolment is brute-force throttled too
    device.throttle_reset()
    resp = client.post(
        "/api/v1/auth/2fa/confirm", {"token": token_for(device)}, content_type="application/json"
    )
    assert resp.status_code == 200
    assert resp.json()["fully_authenticated"] is True
    assert client.get("/protected").status_code == 200
    device.refresh_from_db()
    assert device.confirmed is True
    assert AuditLog.objects.filter(action="auth.2fa_enrolled").exists()


def test_setup_refused_once_enrolled(client):
    user = make_user("admin@example.com", Role.ADMIN)
    TOTPDevice.objects.create(user=user, name="x", confirmed=True)
    login(client, "admin@example.com")
    assert client.post("/api/v1/auth/2fa/setup").status_code == 409


def test_enrolled_user_must_verify_each_login(client):
    user = make_user("admin@example.com", Role.ADMIN)
    device = TOTPDevice.objects.create(user=user, name="x", confirmed=True)
    body = login(client, "admin@example.com").json()
    assert body["two_factor"] == {"required": True, "enrolled": True, "verified": False}
    assert client.get("/protected").status_code == 403

    bad = client.post("/api/v1/auth/2fa/verify", {"token": "123456"}, content_type="application/json")
    assert bad.status_code == 400
    assert AuditLog.objects.filter(action="auth.2fa_failed").exists()

    # django-otp backs off after a failure (1s, 2s, 4s...); skip the wait instead of sleeping.
    device.refresh_from_db()
    assert device.verify_is_allowed()[0] is False
    device.throttle_reset()
    ok = client.post("/api/v1/auth/2fa/verify", {"token": token_for(device)}, content_type="application/json")
    assert ok.status_code == 200
    assert client.get("/protected").status_code == 200


def test_agent_who_opted_into_2fa_must_use_it(client):
    user = make_user()
    TOTPDevice.objects.create(user=user, name="x", confirmed=True)
    body = login(client, "agent@example.com").json()
    assert body["fully_authenticated"] is False
    assert client.get("/protected").status_code == 403


def test_2fa_endpoints_need_password_login_first(client):
    assert client.post("/api/v1/auth/2fa/setup").status_code == 403
    assert (
        client.post(
            "/api/v1/auth/2fa/verify", {"token": "123456"}, content_type="application/json"
        ).status_code
        == 403
    )


# --- capabilities -------------------------------------------------------------------------------


def test_capability_enforced(client):
    make_user()
    login(client, "agent@example.com")
    assert client.get("/staff-only").status_code == 403


def test_super_admin_with_verified_2fa_has_capability(client):
    user = make_user("root@example.com", Role.SUPER_ADMIN)
    device = TOTPDevice.objects.create(user=user, name="x", confirmed=True)
    login(client, "root@example.com")
    assert client.get("/staff-only").status_code == 403  # password alone is not enough
    client.post("/api/v1/auth/2fa/verify", {"token": token_for(device)}, content_type="application/json")
    assert client.get("/staff-only").status_code == 200

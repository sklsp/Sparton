"""Error answers of the auth API, each through the client fixture.

Every refusal in app/api/auth.py is pinned here with its exact status and
detail: duplicate email, wrong password, rate limit, a disabled account, an
invalid or expired reset token, and so on. The happy paths live in
test_recovery_paths.py and test_route_coverage.py; this file keeps the error
surface of the module under test.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.auth.tokens import hash_token
from app.core.database.billing_models import AuthToken
from app.core.database.identity import User
from app.core.database.models import utcnow


def _register(client, email: str = "auth@example.com",
              password: str = "correct-horse-battery") -> dict:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": password,
              "organization_name": "Auth Test Org"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestRegisterErrors:
    def test_duplicate_email_is_a_conflict(self, client, db_session):
        _register(client)
        again = client.post(
            "/auth/register",
            json={"email": "auth@example.com", "password": "another-password-1",
                  "organization_name": "Second Org"},
        )
        assert again.status_code == 409
        assert again.json()["detail"] == "Email already registered"

    def test_rate_limit_refuses_burst_signups(self, client, db_session):
        """Public signup is rate limited per IP; a burst must hit the wall."""
        from app.core.config import settings

        refused = False
        for i in range(settings.rate_limit_register_per_minute + 1):
            response = client.post(
                "/auth/register",
                json={"email": f"burst{i}@example.com", "password": "long-password-1",
                      "organization_name": f"Burst {i}"},
            )
            if response.status_code == 429:
                refused = True
                break
        assert refused, "register was never rate limited"

    def test_a_mail_failure_does_not_fail_the_signup(self, client, db_session, monkeypatch):
        """A mail-server outage must not look like 'your account was not created':
        the signup still succeeds and its session works."""
        from app.api import auth as auth_api

        def boom(address: str, token: str) -> None:
            raise RuntimeError("smtp down")

        monkeypatch.setattr(auth_api.email, "send_verification", boom)
        response = client.post(
            "/auth/register",
            json={"email": "nomail@example.com", "password": "correct-horse-battery",
                  "organization_name": "No Mail"},
        )
        assert response.status_code == 201, response.text

        user = db_session.execute(
            select(User).where(User.email == "nomail@example.com")
        ).scalar_one()
        assert user.id is not None

        me = client.get("/auth/me", headers=_headers(response.json()["token"]))
        assert me.status_code == 200


class TestLoginErrors:
    def test_a_wrong_password_is_refused(self, client, db_session):
        _register(client)
        response = client.post(
            "/auth/login", json={"email": "auth@example.com", "password": "wrong-password"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid email or password"

    def test_an_unknown_email_gets_the_same_answer(self, client, db_session):
        """No existence oracle: a typo'd address must not get a different error."""
        _register(client)
        response = client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid email or password"

    def test_a_disabled_account_is_refused(self, client, db_session):
        _register(client)
        user = db_session.execute(
            select(User).where(User.email == "auth@example.com")
        ).scalar_one()
        user.is_active = False
        db_session.commit()

        response = client.post(
            "/auth/login", json={"email": "auth@example.com",
                                 "password": "correct-horse-battery"}
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Account disabled"


class TestLogoutErrors:
    def test_no_session_is_a_clean_401(self, client, db_session):
        """Not a 500 and not a 200: there is nothing to revoke."""
        response = client.post("/auth/logout")
        assert response.status_code == 401
        assert response.json()["detail"] == "No session presented"


class TestVerificationErrors:
    def test_an_already_verified_account_is_not_resent(
        self, client, auth_headers, db_session, monkeypatch
    ):
        """An already-verified account gets sent=false without error, and no new
        link is issued to an address that does not need one."""
        from app.api import auth as auth_api

        sent: list = []
        monkeypatch.setattr(
            auth_api.email, "send_verification", lambda a, t: sent.append((a, t))
        )
        user = db_session.execute(
            select(User).where(User.email == "admin@example.com")
        ).scalar_one()
        user.email_verified = True
        db_session.commit()

        response = client.post("/auth/resend-verification", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["sent"] is False
        assert response.json()["message"] == "Your email is already verified"
        assert sent == [], "no new link may be issued to a verified address"


class TestForgotPasswordErrors:
    def test_an_unknown_address_gets_the_same_answer(self, client, db_session):
        """The endpoint cannot confirm whether an account exists."""
        response = client.post(
            "/auth/forgot-password", json={"email": "ghost@example.com"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["sent"] is True
        assert (
            body["message"]
            == "If that address has a Sparton account, a reset link is on its way."
        )


class TestResetPasswordErrors:
    def _issue_reset_token(self, client, monkeypatch) -> str:
        from app.api import auth as auth_api

        sent: list = []
        monkeypatch.setattr(
            auth_api.email, "send_password_reset", lambda a, t: sent.append((a, t))
        )
        response = client.post(
            "/auth/forgot-password", json={"email": "auth@example.com"}
        )
        assert response.status_code == 200, response.text
        return sent[-1][1]

    def test_an_invalid_reset_token_is_refused(self, client, db_session):
        _register(client)
        response = client.post(
            "/auth/reset-password",
            json={"token": "not-a-real-token", "password": "brand-new-password"},
        )
        assert response.status_code == 400
        assert "invalid or has expired" in response.json()["detail"]

    def test_an_expired_reset_token_is_refused(self, client, db_session, monkeypatch):
        _register(client)
        token = self._issue_reset_token(client, monkeypatch)
        row = db_session.execute(
            select(AuthToken).where(AuthToken.token_hash == hash_token(token))
        ).scalar_one()
        row.expires_at = utcnow() - timedelta(minutes=1)
        db_session.commit()

        response = client.post(
            "/auth/reset-password",
            json={"token": token, "password": "brand-new-password"},
        )
        assert response.status_code == 400
        assert "invalid or has expired" in response.json()["detail"]


class TestChangePasswordErrors:
    def test_a_wrong_current_password_is_refused(self, client, auth_headers):
        response = client.post(
            "/auth/change-password", headers=auth_headers,
            json={"current_password": "not-the-current-one",
                  "new_password": "a-brand-new-password"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Your current password is incorrect"

    def test_repeating_the_current_password_is_refused(self, client, auth_headers):
        response = client.post(
            "/auth/change-password", headers=auth_headers,
            json={"current_password": "correct-horse-battery",
                  "new_password": "correct-horse-battery"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Choose a different password"


class TestMachinePrincipalErrors:
    def test_an_api_key_cannot_update_account_settings(self, client, db_session, monkeypatch):
        """API keys have no account preferences; the route must say so with a 403."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "api_key", "unit-test-machine-key", raising=False)
        response = client.patch(
            "/auth/settings", headers={"X-API-Key": "unit-test-machine-key"},
            json={"weekly_digest_enabled": False},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "API keys have no account preferences"


class TestLanguageErrors:
    def test_a_missing_organization_is_a_404(self, client, auth_headers, db_session):
        """A user whose organization row is gone gets a 404. The pooled engine
        enforces foreign keys on every connection, so the dangling reference can
        only be written over an autocommit connection with checks switched off
        for that one statement."""
        from sqlalchemy import text, update

        from app.core.database.base import engine

        user = db_session.execute(
            select(User).where(User.email == "admin@example.com")
        ).scalar_one()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            conn.execute(update(User).where(User.id == user.id).values(organization_id=99999))
            # Restore the invariant before this connection goes back to the pool.
            conn.execute(text("PRAGMA foreign_keys = ON"))

        response = client.put("/auth/language", headers=auth_headers, json={"language": "nl"})
        assert response.status_code == 404
        assert response.json()["detail"] == "Organization not found"


__all__ = [
    "TestRegisterErrors",
    "TestLoginErrors",
    "TestLogoutErrors",
    "TestVerificationErrors",
    "TestForgotPasswordErrors",
    "TestResetPasswordErrors",
    "TestChangePasswordErrors",
    "TestMachinePrincipalErrors",
    "TestLanguageErrors",
]


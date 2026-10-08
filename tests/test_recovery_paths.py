"""Account recovery must work for an account that cannot yet receive mail.

The bug this file exists to prevent: a verification gate applied to every
`current_user` route makes an unverified customer unable to read their own
account, unable to ask for another link, and unable to change the password they
were emailed about. That is a lockout, not a safeguard.

So: the recovery routes must stay open to an unverified session, and the product
routes must stay shut.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database.identity import User


@pytest.fixture()
def production(monkeypatch):
    """Production is the only mode where the verification gate applies."""
    monkeypatch.setattr(settings, "sparton_env", "production", raising=False)


def mark_unverified(db_session, email: str = "admin@example.com") -> None:
    user = db_session.execute(
        select(User).where(User.email == email)
    ).scalar_one()
    user.email_verified = False
    db_session.commit()


def capture_emails(monkeypatch, *names: str) -> list:
    """Record what would have been sent, so tests never send real mail."""
    from app.api import auth as auth_api

    sent: list = []
    for name in names:
        monkeypatch.setattr(
            auth_api.email, name,
            lambda address, token, _sent=sent: _sent.append((address, token)),
        )
    return sent


class TestRecoveryStaysOpen:
    def test_me_is_reachable_before_verification(self, client, auth_headers,
                                                 db_session, production):
        """The client calls /auth/me on every boot to decide whether to show the
        'confirm your email' banner. If this 403s, the user never learns why."""
        mark_unverified(db_session)
        response = client.get("/auth/me", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["email_verified"] is False

    def test_resend_verification_is_reachable_before_verification(
        self, client, auth_headers, db_session, production, monkeypatch
    ):
        capture_emails(monkeypatch, "send_verification")
        mark_unverified(db_session)
        response = client.post("/auth/resend-verification", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["sent"] is True

    def test_change_password_is_reachable_before_verification(
        self, client, auth_headers, db_session, production
    ):
        """The emailed password is exactly the one a stuck user needs to change.
        It still requires the current password, so this is not a takeover."""
        mark_unverified(db_session)
        response = client.post(
            "/auth/change-password",
            headers=auth_headers,
            json={
                "current_password": "correct-horse-battery",
                "new_password": "a-brand-new-password",
            },
        )
        assert response.status_code == 200

    def test_a_verified_session_still_reaches_recovery(
        self, client, auth_headers, production
    ):
        """The exemption is for unverified users, not a second auth mechanism:
        a verified session must not be rejected by these routes."""
        assert client.get("/auth/me", headers=auth_headers).status_code == 200
        assert client.post(
            "/auth/change-password",
            headers=auth_headers,
            json={
                "current_password": "correct-horse-battery",
                "new_password": "yet-another-password",
            },
        ).status_code == 200


class TestProductStaysShut:
    """The other half of the guarantee: recovery is open, tenant data is not."""

    @pytest.mark.parametrize(
        "path", ["/shops", "/overview", "/changes", "/reports", "/billing/plan"]
    )
    def test_unverified_users_are_refused_product_access(
        self, client, auth_headers, db_session, production, path
    ):
        mark_unverified(db_session)
        response = client.get(path, headers=auth_headers)
        assert response.status_code == 403
        assert "confirm your email" in response.json()["detail"].lower()

    def test_recovery_routes_still_require_authentication(self, client, production):
        """Anonymous callers get 401. Opening recovery to unverified users must
        not have opened it to everybody."""
        for method, path in (
            ("get", "/auth/me"),
            ("post", "/auth/resend-verification"),
            ("post", "/auth/change-password"),
        ):
            kwargs = {"json": {}} if method == "post" else {}
            assert getattr(client, method)(path, **kwargs).status_code == 401


class TestVerificationRoundTrip:
    def test_verifying_a_token_lets_the_user_in(self, client, production,
                                                 monkeypatch):
        """The end the whole flow exists for: redeem a token, then use the app."""
        sent = capture_emails(monkeypatch, "send_verification")

        registered = client.post(
            "/auth/register",
            json={
                "email": "roundtrip@example.com",
                "password": "correct-horse-battery",
                "organization_name": "Round Trip",
            },
        )
        assert registered.status_code == 201
        headers = {"Authorization": f"Bearer {registered.json()['token']}"}

        # Unverified: the product is closed.
        assert client.get("/shops", headers=headers).status_code == 403

        # A link really was sent, and redeeming it works.
        assert sent, "no verification email was sent"
        assert client.post(
            "/auth/verify-email", json={"token": sent[-1][1]}
        ).status_code == 200

        # Verified: open.
        assert client.get("/shops", headers=headers).status_code == 200

    def test_a_dead_link_says_so_and_unlocks_nothing(self, client, production,
                                                    monkeypatch, db_session):
        """An expired, used or unknown link gets a 400. It used to answer
        "verified", so the page told someone with an expired link that the
        account was fully active while the product kept refusing them."""
        from datetime import timedelta

        from app.core.auth.tokens import hash_token
        from app.core.database.billing_models import AuthToken
        from app.core.database.models import utcnow

        sent = capture_emails(monkeypatch, "send_verification")
        registered = client.post(
            "/auth/register",
            json={
                "email": "late@example.com",
                "password": "correct-horse-battery",
                "organization_name": "Late Click",
            },
        )
        headers = {"Authorization": f"Bearer {registered.json()['token']}"}

        # The link is opened a day too late.
        row = db_session.execute(
            select(AuthToken).where(AuthToken.token_hash == hash_token(sent[-1][1]))
        ).scalar_one()
        row.expires_at = utcnow() - timedelta(minutes=1)
        db_session.commit()
        for dead in (sent[-1][1], "not-a-real-token"):
            response = client.post("/auth/verify-email", json={"token": dead})
            assert response.status_code == 400
            assert "no longer works" in response.json()["detail"]
        assert client.get("/shops", headers=headers).status_code == 403

        # A new link works once.
        assert client.post("/auth/resend-verification", headers=headers).status_code == 200
        fresh = sent[-1][1]
        assert client.post("/auth/verify-email", json={"token": fresh}).status_code == 200
        assert client.post("/auth/verify-email", json={"token": fresh}).status_code == 400
        assert client.get("/shops", headers=headers).status_code == 200

    def test_password_reset_yields_a_working_session(self, client, production,
                                                     monkeypatch):
        sent = capture_emails(monkeypatch, "send_password_reset")

        client.post(
            "/auth/register",
            json={
                "email": "reset@example.com",
                "password": "correct-horse-battery",
                "organization_name": "Reset Me",
            },
        )
        assert client.post(
            "/auth/forgot-password", json={"email": "reset@example.com"}
        ).status_code == 200
        assert sent, "no reset email was sent"

        assert client.post(
            "/auth/reset-password",
            json={"token": sent[-1][1], "password": "brand-new-password"},
        ).status_code == 200

        # The new password is the one that works; the old one no longer is.
        assert client.post(
            "/auth/login",
            json={"email": "reset@example.com", "password": "brand-new-password"},
        ).status_code == 200
        assert client.post(
            "/auth/login",
            json={"email": "reset@example.com", "password": "correct-horse-battery"},
        ).status_code == 401

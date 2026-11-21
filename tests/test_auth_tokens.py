"""Tests for app/core/auth/tokens.py: one-time verification and reset tokens.

Covers the branches the billing suite does not reach: an unknown purpose, a
token redeemed by an inactive user, expiry at redemption time, and the
scheduler's purge of used and old rows.
"""

from __future__ import annotations

from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import tokens
from app.core.auth.service import hash_password
from app.core.database.billing_models import AuthToken
from app.core.database.identity import Organization, User
from app.core.database.models import utcnow


def _make_user(db_session, email="tok@example.com", is_active=True) -> User:
    # The slug comes from the local part of the email so one test can hold
    # several users without hitting the unique-slug constraint.
    org = Organization(name=f"Token Test {email}", slug=email.split("@")[0])
    db_session.add(org)
    db_session.flush()
    user = User(
        organization_id=org.id,
        email=email,
        password_hash=hash_password("long-password-1"),
        is_active=is_active,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _row_for(db_session: Session, token: str) -> AuthToken:
    return db_session.execute(
        select(AuthToken).where(AuthToken.token_hash == tokens.hash_token(token))
    ).scalars().one()


class TestIssue:
    def test_issue_rejects_an_unknown_purpose(self, db_session):
        """A typo in the purpose must fail loudly, not mint a useless token."""
        user = _make_user(db_session)
        with pytest.raises(ValueError, match="Unknown token purpose"):
            tokens.issue(db_session, user, "delete_account")
        # Nothing is written for a rejected purpose.
        assert db_session.execute(select(AuthToken)).scalars().all() == []

    def test_issue_returns_a_urlsafe_token_stored_hashed(self, db_session):
        """The plaintext goes in the email; only its SHA-256 touches the DB."""
        user = _make_user(db_session)
        plain = tokens.issue(db_session, user, "verify_email")
        row = db_session.execute(select(AuthToken)).scalars().one()
        assert row.token_hash == tokens.hash_token(plain)
        assert plain not in row.token_hash


class TestConsume:
    def test_consume_returns_the_user_and_stamps_used_at(self, db_session):
        user = _make_user(db_session)
        token = tokens.issue(db_session, user, "reset_password")
        got = tokens.consume(db_session, token, "reset_password")
        assert got is not None and got.id == user.id
        row = _row_for(db_session, token)
        assert row.used_at is not None

    def test_consume_rejects_a_token_for_an_inactive_user(self, db_session):
        """A deactivated account must not be able to redeem its reset link."""
        user = _make_user(db_session, is_active=False)
        token = tokens.issue(db_session, user, "reset_password")
        assert tokens.consume(db_session, token, "reset_password") is None
        # Deactivation is a refusal, not a redemption: the row stays unused.
        row = _row_for(db_session, token)
        assert row.used_at is None

    def test_consume_rejects_an_expired_token(self, db_session):
        user = _make_user(db_session)
        token = tokens.issue(db_session, user, "verify_email")
        row = _row_for(db_session, token)
        row.expires_at = utcnow() - timedelta(minutes=1)
        db_session.commit()
        assert tokens.consume(db_session, token, "verify_email") is None


class TestIsVerified:
    def test_is_verified_allows_everyone_when_verification_is_off(self, db_session):
        # The suite runs outside production, so the flag defaults to off.
        user = _make_user(db_session)
        assert tokens.is_verified(user) is True

    def test_is_verified_checks_the_flag_when_required(self, monkeypatch, db_session):
        from app.core.config import settings

        monkeypatch.setattr(settings, "email_verification_required", True)
        user = _make_user(db_session)
        assert tokens.is_verified(user) is False
        tokens.mark_verified(db_session, user)
        assert user.email_verified is True
        assert user.email_verified_at is not None
        assert tokens.is_verified(user) is True

    def test_is_verified_refuses_a_principal_without_the_attribute(self, monkeypatch):
        """A machine principal has no email_verified field at all."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "email_verification_required", True)
        # Deliberately not a User: is_verified must refuse principals without the field.
        assert tokens.is_verified(cast(User, object())) is False


class TestPurgeExpired:
    def test_purge_deletes_used_and_old_tokens(self, db_session):
        user = _make_user(db_session)
        fresh = tokens.issue(db_session, user, "verify_email")
        used = tokens.issue(db_session, user, "reset_password")
        assert tokens.consume(db_session, used, "reset_password") is not None

        # An expired but never-used token: backdate its expiry past the 7-day cutoff.
        old_user = _make_user(db_session, email="old@example.com")
        old = tokens.issue(db_session, old_user, "verify_email")
        row = _row_for(db_session, old)
        row.expires_at = utcnow() - timedelta(days=8)
        db_session.commit()

        assert tokens.purge_expired(db_session) == 2
        remaining = db_session.execute(select(AuthToken)).scalars().all()
        assert len(remaining) == 1
        assert remaining[0].token_hash == tokens.hash_token(fresh)

    def test_purge_returns_zero_when_everything_is_fresh(self, db_session):
        user = _make_user(db_session)
        tokens.issue(db_session, user, "verify_email")
        assert tokens.purge_expired(db_session) == 0
        remaining = db_session.execute(select(AuthToken)).scalars().all()
        assert len(remaining) == 1

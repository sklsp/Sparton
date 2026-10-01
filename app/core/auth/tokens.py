"""One-time tokens for email verification and password reset.

Both flows share this module (D-011). The invariants:

* The token is generated with ``secrets`` (32 bytes, URL-safe) and stored
  **hashed** with SHA-256, exactly like a session token. A database dump
  therefore does not hand an attacker working reset links.
* A token is single-use: consuming it stamps ``used_at``.
* Issuing a new token **invalidates the previous one for the same purpose**,
  so a forwarded old email cannot be used after a re-request.
* Nothing here ever raises for a "no such token" — callers get ``None`` and
  answer with a message that does not confirm whether the account exists.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database.billing_models import AuthToken, AuthTokenPurpose
from app.core.database.identity import User
from app.core.database.models import utcnow

logger = logging.getLogger(__name__)

#: 32 random bytes, URL-safe. Same order of magnitude as a session token.
TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(
    db: Session, user: User, purpose: str
) -> str:
    """Create a fresh token for `user`, invalidating any previous one.

    Returns the **plaintext** token, which the caller must put in an email.
    It is not recoverable afterwards.
    """
    if purpose not in AuthTokenPurpose.ALL:
        raise ValueError(f"Unknown token purpose: {purpose!r}")

    # Burn the old one first, so a re-request always wins.
    db.execute(
        update(AuthToken)
        .where(AuthToken.user_id == user.id, AuthToken.purpose == purpose)
        .values(used_at=utcnow())
    )
    db.commit()

    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(
        AuthToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_token(token),
            expires_at=utcnow() + timedelta(minutes=settings.auth_token_ttl_minutes),
        )
    )
    db.commit()
    return token


def consume(db: Session, token: str, purpose: str) -> User | None:
    """Redeem a token. Returns the user on success, ``None`` on any failure.

    Every rejection path is indistinguishable to the caller, so this cannot be
    used to enumerate which token hashes or user ids exist.
    """
    if not token:
        return None
    row = db.execute(
        select(AuthToken).where(
            AuthToken.token_hash == hash_token(token),
            AuthToken.purpose == purpose,
        )
    ).scalars().first()
    if row is None or not row.is_usable:
        return None

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None

    row.used_at = utcnow()
    db.commit()
    return user


def is_verified(user) -> bool:
    """Is this principal allowed to use the product?

    When verification is not required — local development, and the test suite —
    everyone is allowed. In production an unverified account is refused at the
    product boundary, not at signup, so a customer can still sign in and resend
    the link (D-010).
    """
    if not settings.require_email_verification:
        return True
    return bool(getattr(user, "email_verified", False))


def mark_verified(db: Session, user: User) -> None:
    user.email_verified = True
    user.email_verified_at = utcnow()
    db.commit()


def purge_expired(db: Session) -> int:
    """Delete tokens that are used or expired. Called by the scheduler."""
    from datetime import datetime, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    result = db.execute(
        AuthToken.__table__.delete().where(
            (AuthToken.used_at.isnot(None)) | (AuthToken.expires_at < cutoff)
        )
    )
    db.commit()
    return result.rowcount or 0


__all__ = [
    "TOKEN_BYTES",
    "consume",
    "hash_token",
    "is_verified",
    "issue",
    "mark_verified",
    "purge_expired",
]

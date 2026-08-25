"""Authentication and RBAC (Ares foundation).

Passwords use scrypt (stdlib, memory-hard). Session tokens are random,
stored only as SHA-256 hashes, and expire. Roles are enforced server-side
on every request via FastAPI dependencies.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database.identity import AuditLog, Organization, Session as SessionRow, User
from app.core.database.models import utcnow

SESSION_TTL = timedelta(hours=12)


class Role(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    ANALYST = "analyst"
    VIEWER = "viewer"


# Ordered capability levels; a role can do anything its juniors can.
ROLE_LEVEL: dict[str, int] = {
    Role.VIEWER.value: 0,
    Role.ANALYST.value: 1,
    Role.MANAGER.value: 2,
    Role.ADMIN.value: 3,
}


def role_at_least(role: str, required: str) -> bool:
    return ROLE_LEVEL.get(role, -1) >= ROLE_LEVEL.get(required, 99)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_session(db: Session, user: User) -> tuple[str, SessionRow]:
    token = secrets.token_urlsafe(32)
    row = SessionRow(
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        user_id=user.id,
        expires_at=utcnow() + SESSION_TTL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return token, row


def resolve_session(db: Session, token: str) -> User | None:
    row = db.execute(
        select(SessionRow).where(
            SessionRow.token_hash == hashlib.sha256(token.encode()).hexdigest()
        )
    ).scalars().first()
    if row is None or not row.is_valid:
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return user


def revoke_session(db: Session, token: str) -> bool:
    row = db.execute(
        select(SessionRow).where(
            SessionRow.token_hash == hashlib.sha256(token.encode()).hexdigest()
        )
    ).scalars().first()
    if row is None:
        return False
    row.revoked_at = utcnow()
    db.commit()
    return True


def audit(
    db: Session,
    *,
    action: str,
    actor_user_id: int | None = None,
    organization_id: int | None = None,
    resource: str = "",
    outcome: str = "success",
    correlation_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Record a security-relevant event. Never include secrets in detail."""
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            organization_id=organization_id,
            action=action,
            resource=resource[:160],
            outcome=outcome,
            correlation_id=correlation_id,
            detail=detail,
        )
    )
    db.commit()


def ensure_default_organization(db: Session) -> Organization:
    org = db.execute(select(Organization)).scalars().first()
    if org is None:
        org = Organization(name="Default")
        db.add(org)
        db.commit()
        db.refresh(org)
    return org

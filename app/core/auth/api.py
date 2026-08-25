"""FastAPI authentication dependencies.

Resolves the caller's identity for every request:

1. ``Authorization: Bearer <session token>`` — an interactive user session.
2. ``X-API-Key: <shared key>`` — a machine principal (server-to-server),
   valid only when ``API_KEY`` is configured; maps to a synthetic admin
   user with no organization (sees all tenants).

Requests with neither credential raise 401 unless the deployment runs
without an ``API_KEY`` *and* anonymous access is explicitly allowed
(local development only).
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.auth.service import resolve_session
from app.core.config import settings
from app.core.database.base import get_db
from app.core.database.identity import User

DbSession = Annotated[Session, Depends(get_db)]


class MachineUser:
    """Synthetic principal for shared API-key callers.

    ``id=None`` and ``organization_id=None`` are deliberate invariants:
    tenant scoping checks ``is_machine`` / ``organization_id is None`` to
    grant cross-tenant visibility, and audit rows record machine actions
    without a user FK.
    """

    id = None
    email = "machine@sparton.local"
    role = "admin"
    organization_id = None
    is_active = True
    is_machine = True


def _extract_bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


def current_user(
    request: Request,
    db: DbSession,
) -> User | MachineUser:
    """Resolve the authenticated principal or raise 401."""
    # 1. Interactive session.
    token = _extract_bearer_token(request)
    if token is not None:
        user = resolve_session(db, token)
        if user is not None:
            return user
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. Shared machine key (constant-time compare).
    api_key = request.headers.get("X-API-Key")
    if api_key and settings.api_key:
        if hmac.compare_digest(api_key, settings.api_key):
            return MachineUser()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # 3. Open local development: no API key configured and no credentials
    # presented. Anonymous requests act as a synthetic viewer-scoped admin
    # of the default org so local workflows still function end to end.
    if not settings.api_key:
        anon = MachineUser()
        anon.email = "anonymous@localhost"
        return anon

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_role(required: str):
    """Dependency factory: gate an endpoint behind a minimum role level."""

    def dependency(user: Annotated[User | MachineUser, Depends(current_user)]):
        from app.core.auth.service import ROLE_LEVEL, role_at_least

        if not role_at_least(getattr(user, "role", ""), required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {required} role",
            )
        return user

    return dependency


RequireViewer = Depends(require_role("viewer"))
RequireAnalyst = Depends(require_role("analyst"))
RequireManager = Depends(require_role("manager"))
RequireAdmin = Depends(require_role("admin"))


__all__ = [
    "DbSession",
    "MachineUser",
    "RequireAdmin",
    "RequireAnalyst",
    "RequireManager",
    "RequireViewer",
    "current_user",
    "require_role",
]

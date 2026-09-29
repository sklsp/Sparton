"""FastAPI authentication dependencies.

Resolves the caller's identity for every request:

1. ``Authorization: Bearer <session token>`` — an interactive user session.
2. ``X-API-Key: <shared key>`` — a machine principal (server-to-server),
   valid only when ``API_KEY`` is configured; maps to a synthetic admin
   user with no organization (sees all tenants).

Requests with neither credential raise 401. There is no anonymous fallback: an
unset ``API_KEY`` disables the machine principal rather than opening the API
(see docs/DECISIONS.md D-004).
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
from app.llm import set_usage_context

DbSession = Annotated[Session, Depends(get_db)]


class MachineUser:
    """Synthetic principal for shared API-key callers.

    ``id=None`` and ``organization_id=None`` are deliberate invariants:
    tenant scoping checks ``is_machine`` / ``organization_id is None`` to
    grant cross-tenant visibility, and audit rows record machine actions
    without a user FK.

    Use :meth:`create` rather than the constructor: a bare instance carries a
    mutable ``email`` and sharing one class-level object across requests lets
    one caller overwrite another's identity.
    """

    id = None
    email = "machine@sparton.local"
    role = "admin"
    organization_id = None
    is_active = True
    is_machine = True

    def __init__(self, email: str = "machine@sparton.local") -> None:
        # Instance attribute: shadowing the class default, not mutating it.
        self.email = email


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
    """Resolve the authenticated principal or raise 401.

    Also binds the tenant into a context variable so LLM calls made deeper in
    the stack are attributed to the right organization for billing, without
    threading a user object through every call signature. This runs as a route
    dependency, i.e. immediately before the handler, which is why it lives
    here rather than in request middleware.
    """
    principal = _authenticate(request, db)
    request.state.user = principal
    set_usage_context(
        getattr(principal, "organization_id", None), getattr(principal, "id", None)
    )
    _require_verified(principal)
    return principal


def _require_verified(principal) -> None:
    """Refuse product access to an unverified account, in production only.

    Deliberately *not* enforced at signup: a customer must be able to sign in,
    see the banner and request a new link, or they are locked out of their own
    account with no way to recover (D-010).
    """
    from app.core.auth import tokens

    if tokens.is_verified(principal):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "Confirm your email address to use Sparton. "
            "Check your inbox, or request a new link from the dashboard."
        ),
    )


def _authenticate(request: Request, db: Session) -> User | MachineUser:
    """1. Interactive session, 2. shared machine key, 3. 401."""
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

    # 3. No usable credential.
    #
    # There is deliberately NO "no API_KEY configured, so let everyone in"
    # fallback. MachineUser.organization_id is None, which every query path
    # reads as "sees all tenants" — so an unset API_KEY used to turn a
    # forgotten env var into a public cross-tenant read/write window.
    # Local development registers a user like any other client.
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

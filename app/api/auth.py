"""Authentication API: register, login, logout, me."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.core.auth.api import DbSession, current_user
from app.core.auth.service import (
    audit,
    create_session,
    hash_password,
    revoke_session,
    verify_password,
)
from app.core.config import settings
from app.core.database.identity import Organization, User
from app.core.observability.metrics import inc
from app.core.security.rate_limit import rate_limit
from app.api.schemas import LoginRequest, RegisterRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

def _user_dict(user) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "organization_id": user.organization_id,
    }


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(payload: RegisterRequest, db: DbSession) -> TokenResponse:
    existing = db.execute(
        select(User).where(User.email == payload.email)
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered")

    org = db.execute(
        select(Organization).where(Organization.name == payload.organization_name)
    ).scalars().first()
    if org is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Organization already exists; ask an admin for an invite",
        )
    org = Organization(name=payload.organization_name)
    db.add(org)
    db.flush()

    user = User(
        organization_id=org.id,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="admin",  # first user of a new org is its admin
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token, _ = create_session(db, user)
    audit(db, action="auth.register", actor_user_id=user.id, organization_id=user.organization_id)
    return TokenResponse(token=token, user=_user_dict(user))


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    db: DbSession,
    _: None = Depends(
        rate_limit(limit=settings.rate_limit_login_per_minute)
    ),
) -> TokenResponse:
    user = db.execute(select(User).where(User.email == payload.email)).scalars().first()
    if user is None or not verify_password(payload.password, user.password_hash):
        inc("auth_failures_total")
        audit(db, action="auth.login_failed", resource=payload.email[:60], outcome="denied")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Account disabled")

    token, _ = create_session(db, user)
    audit(db, action="auth.login", actor_user_id=user.id, organization_id=user.organization_id)
    return TokenResponse(token=token, user=_user_dict(user))


@router.post("/logout")
def logout(request: Request, db: DbSession) -> dict:
    creds: HTTPAuthorizationCredentials | None = _bearer(request)
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="No session presented")
    revoked = revoke_session(db, creds.credentials)
    return {"revoked": revoked}


@router.get("/me")
def me(user: Annotated[object, Depends(current_user)]) -> dict:
    return _user_dict(user)


__all__ = ["router"]

"""Authentication API: register, login, logout, me."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from app.core import email
from app.core.auth import tokens
from app.core.auth.api import DbSession, current_user, unverified_user
from app.core.auth.service import (
    audit,
    create_session,
    hash_password,
    revoke_session,
    unique_slug,
    verify_password,
)
from app.core.config import settings
from app.core.database.billing_models import AuthTokenPurpose
from app.core.database.identity import Organization, Session, User
from app.core.observability.metrics import inc
from app.core.security.rate_limit import rate_limit
from app.api.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserSettingsUpdate,
    VerifyRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])

def _user_dict(user) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "organization_id": user.organization_id,
        "email_verified": bool(getattr(user, "email_verified", False)),
        # D-032 account preferences. getattr: machine principals have no such
        # columns and /me must still answer for them.
        "language": getattr(user, "language", None) or "nl",
        "weekly_digest_enabled": bool(getattr(user, "weekly_digest_enabled", True)),
    }


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(
    payload: RegisterRequest,
    request: Request,
    db: DbSession,
    _: None = Depends(rate_limit(limit=settings.rate_limit_register_per_minute)),
) -> TokenResponse:
    # Public signup: rate limited per IP. Without this, anyone can fill the
    # database with organizations at line speed.
    existing = db.execute(
        select(User).where(User.email == payload.email)
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered")

    # Organization display names are NOT unique: on a public signup form a
    # global unique constraint lets one user squat "Acme" and deny it to every
    # other shop. Uniqueness lives on the slug instead (D-009, D-026).
    org = Organization(
        name=payload.organization_name,
        slug=unique_slug(db, payload.organization_name),
    )
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

    # Send the verification link. A failure here must not fail the signup: the
    # customer can always resend, and a mail-server outage should not look like
    # "your account was not created".
    try:
        verification_token = tokens.issue(db, user, AuthTokenPurpose.VERIFY_EMAIL)
        email.send_verification(user.email, verification_token)
    except Exception:  # noqa: BLE001 - never lose a successful signup to mail
        import logging

        logging.getLogger(__name__).warning(
            "Could not send the verification email to %s", user.email, exc_info=True
        )

    response = _user_dict(user)
    response["verification_required"] = settings.require_email_verification
    return TokenResponse(token=token, user=response)


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
    """Revoke the caller's own session.

    Reads the header directly rather than via FastAPI's `HTTPBearer`: its
    `__call__` is async, so calling it from a sync handler returns a coroutine
    instead of the credentials, and `creds.credentials` raises `AttributeError` —
    a 500 on the one route a user hits when something has already gone wrong.
    """
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="No session presented",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"revoked": revoke_session(db, token)}


@router.get("/me")
def me(user: Annotated[object, Depends(unverified_user)]) -> dict:
    """Who am I.

    Uses `unverified_user`, not `current_user`: the client calls this on every
    boot to decide whether to show the "confirm your email" banner. Gating it
    behind verification would make an unverified customer unable to discover
    that they need verifying.
    """
    return _user_dict(user)


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------
@router.post("/verify-email", status_code=200)
def verify_email(payload: VerifyRequest, db: DbSession = None) -> dict:
    """Redeem a verification link.

    The token is single-use, and redeeming it marks the user verified. A token
    that has already been used reports success anyway: the honest answer is
    "this link is not valid any more", and telling a second visitor that their
    link already worked is not worth the enumeration surface.
    """
    user = tokens.consume(db, payload.token, AuthTokenPurpose.VERIFY_EMAIL)
    if user is not None:
        tokens.mark_verified(db, user)
        audit(
            db, action="auth.email_verified", actor_user_id=user.id,
            organization_id=user.organization_id,
        )
    return {"verified": True}


@router.post("/resend-verification")
def resend_verification(
    request: Request,
    db: DbSession = None,
    user: Annotated[object, Depends(unverified_user)] = None,
) -> dict:
    """Send the verification link again.

    `unverified_user` is the whole point of this route: requiring a verified
    email to ask for a verification email is a deadlock.
    """
    if getattr(user, "email_verified", False):
        return {"sent": False, "message": "Your email is already verified"}
    token = tokens.issue(db, user, AuthTokenPurpose.VERIFY_EMAIL)
    email.send_verification(user.email, token)
    audit(
        db, action="auth.verification_resent", actor_user_id=user.id,
        organization_id=user.organization_id,
    )
    return {"sent": True}


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
@router.post("/forgot-password")
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: DbSession = None,
    _: None = Depends(rate_limit(limit=settings.rate_limit_forgot_password_per_minute)),
) -> dict:
    """Start a password reset.

    Always returns the same thing, whether or not the address exists. Anything
    else turns this endpoint into a way to enumerate who has an account.
    """
    user = db.execute(
        select(User).where(User.email == payload.email, User.is_active.is_(True))
    ).scalars().first()
    if user is not None:
        token = tokens.issue(db, user, AuthTokenPurpose.RESET_PASSWORD)
        email.send_password_reset(user.email, token)
        audit(
            db, action="auth.password_reset_requested",
            organization_id=user.organization_id, resource="redacted",
        )
    return {
        "sent": True,
        "message": "If that address has a Sparton account, a reset link is on its way.",
    }


@router.post("/reset-password")
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    db: DbSession = None,
    _: None = Depends(rate_limit(limit=settings.rate_limit_forgot_password_per_minute)),
) -> dict:
    """Complete a password reset.

    On success every existing session for that user is revoked: if the reset
    was prompted by a compromise, the attacker's sessions must not survive it.
    """
    user = tokens.consume(db, payload.token, AuthTokenPurpose.RESET_PASSWORD)
    if user is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="That reset link is invalid or has expired. Request a new one.",
        )

    user.password_hash = hash_password(payload.password)
    db.commit()
    db.execute(
        Session.__table__.delete().where(Session.__table__.c.user_id == user.id)
    )
    db.commit()
    audit(
        db, action="auth.password_reset", actor_user_id=user.id,
        organization_id=user.organization_id,
    )
    return {"reset": True, "message": "Password updated. Sign in with your new password."}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    db: DbSession = None,
    user: Annotated[object, Depends(unverified_user)] = None,
) -> dict:
    """Change your own password. Requires the current one.

    `unverified_user`: the password being changed is the one the customer was
    emailed about. Gating this behind verification would lock out exactly the
    person who cannot receive mail yet. It still requires the *current*
    password, so this is not a takeover vector.
    """
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Your current password is incorrect"
        )
    if payload.current_password == payload.new_password:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Choose a different password"
        )
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    audit(
        db, action="auth.password_changed", actor_user_id=user.id,
        organization_id=user.organization_id,
    )
    return {"changed": True}


@router.patch("/settings")
def update_settings(
    payload: UserSettingsUpdate,
    db: DbSession = None,
    user: Annotated[object, Depends(unverified_user)] = None,
) -> dict:
    """Update your own account preferences: UI language and the weekly email digest.

    ``unverified_user`` on purpose (like change-password): these are personal
    preferences of your own account, not tenant data, and a freshly signed-up
    customer should be able to set them before verifying their address. The
    digest itself only ever goes out to verified addresses.
    """
    if getattr(user, "is_machine", False):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="API keys have no account preferences")
    if payload.language is not None:
        user.language = payload.language
    if payload.weekly_digest_enabled is not None:
        user.weekly_digest_enabled = payload.weekly_digest_enabled
    db.commit()
    audit(
        db, action="auth.settings_updated", actor_user_id=user.id,
        organization_id=user.organization_id,
    )
    return _user_dict(user)


__all__ = ["router"]

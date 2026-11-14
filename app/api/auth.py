"""Authentication API: register, login, logout, me."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import Table, select

from app.api.responses import errors
from app.api.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LanguageRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserSettingsUpdate,
    VerifyRequest,
)
from app.core import email
from app.core.auth import tokens
from app.core.auth.api import DbSession, MachineUser, unverified_user
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

router = APIRouter(prefix="/auth", tags=["auth"])

def _user_dict(user: User | MachineUser) -> dict[str, Any]:
    org = getattr(user, "organization", None)
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "organization_id": user.organization_id,
        "email_verified": bool(getattr(user, "email_verified", False)),
        # One language per account (Organization.language, v1.1): reports and the
        # D-032 digest email follow it. getattr: machine principals have neither.
        "language": getattr(org, "language", None) or "en",
        "weekly_digest_enabled": bool(getattr(user, "weekly_digest_enabled", True)),
    }


@router.post("/register", response_model=TokenResponse, status_code=201, responses=errors(409, 429))
def register(
    payload: RegisterRequest,
    request: Request,
    db: DbSession,
    _: None = Depends(rate_limit(limit=settings.rate_limit_register_per_minute)),
) -> TokenResponse:
    """Create a new organization with the caller as its first admin, and sign in.

    Public, but rate-limited per IP. A verification email is sent when the
    deployment requires one; a mail failure never fails the signup.
    """
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
        language=payload.language,
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

    token, _session = create_session(db, user)
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


@router.post("/login", response_model=TokenResponse, responses=errors(401, 403, 429))
def login(
    payload: LoginRequest,
    request: Request,
    db: DbSession,
    _: None = Depends(
        rate_limit(limit=settings.rate_limit_login_per_minute)
    ),
) -> TokenResponse:
    """Sign in with email and password; returns a session token.

    Public, but rate-limited per IP. A disabled account is refused with 403.
    """
    user = db.execute(select(User).where(User.email == payload.email)).scalars().first()
    if user is None or not verify_password(payload.password, user.password_hash):
        inc("auth_failures_total")
        audit(db, action="auth.login_failed", resource=payload.email[:60], outcome="denied")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Account disabled")

    token, _session = create_session(db, user)
    audit(db, action="auth.login", actor_user_id=user.id, organization_id=user.organization_id)
    return TokenResponse(token=token, user=_user_dict(user))


@router.post("/logout", responses=errors(401))
# Reads the header directly rather than via FastAPI's `HTTPBearer`: its `__call__` is async, so calling it from a
# sync handler returns a coroutine instead of the credentials, and `creds.credentials` raises `AttributeError`, a
# 500 on the one route a user hits when something has already gone wrong.
def logout(request: Request, db: DbSession) -> dict[str, Any]:
    """Revoke the caller's own session token.

    An unknown or expired token is not an error: the response says whether
    anything was revoked.
    """
    # The header is read directly rather than via FastAPI's HTTPBearer, whose
    # async __call__ returns a coroutine from sync code (a 500 on this route).
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="No session presented",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"revoked": revoke_session(db, token)}


@router.get("/me", summary="Who am I", responses=errors(401))
# Uses `unverified_user`, not `current_user`: the client calls this on every boot to decide whether to show the
# "confirm your email" banner. Gating it behind verification would make an unverified customer unable to discover
# that they need verifying.
def me(user: Annotated[User | MachineUser, Depends(unverified_user)]) -> dict[str, Any]:
    """The caller's own account details, including whether the email is verified.

    Signed-in members and API keys may call it; it works before verification so
    the dashboard can show the confirm-email banner.
    """
    return _user_dict(user)


@router.put("/language", responses=errors(401, 404))
# Any member may set it: it decides which language the next weekly report is written in, nothing else (D-038).
def set_language(
    payload: LanguageRequest,
    db: DbSession,
    user: Annotated[User | MachineUser, Depends(unverified_user)],
) -> dict[str, Any]:
    """Set the organization's report language, nl or en.

    Any signed-in member may set it: it decides which language the next weekly
    report is written in, nothing else.
    """
    org = db.get(Organization, user.organization_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Organization not found")
    org.language = payload.language
    db.commit()
    return {"language": org.language}


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------
@router.post("/verify-email", status_code=200, responses=errors(400))
# The token is single-use, and redeeming it marks the user verified. An unknown, used or expired link gets one and
# the same 400. It used to report success, which told someone whose link had expired that their account was "fully
# active" while the product kept refusing them. The 400 tells only the holder of a link that this link does not work
# now; tokens are random, so there is nothing to enumerate.
def verify_email(payload: VerifyRequest, db: DbSession) -> dict[str, Any]:
    """Redeem a verification link from the confirmation email.

    The link is single-use and marks the account verified. An unknown, used or
    expired link gets one and the same 400; tokens are random, so there is
    nothing to enumerate.
    """
    user = tokens.consume(db, payload.token, AuthTokenPurpose.VERIFY_EMAIL)
    if user is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="This link no longer works. If the dashboard still asks you to confirm your "
            "email, send a new link from there.",
        )
    tokens.mark_verified(db, user)
    audit(
        db, action="auth.email_verified", actor_user_id=user.id,
        organization_id=user.organization_id,
    )
    return {"verified": True}


@router.post("/resend-verification", responses=errors(401, 403))
# `unverified_user` is the whole point of this route: requiring a verified email to ask for a verification email is
# a deadlock.
def resend_verification(
    request: Request,
    db: DbSession,
    user: Annotated[User | MachineUser, Depends(unverified_user)],
) -> dict[str, Any]:
    """Send the verification link again to the caller's own address.

    Signed-in members only; API keys get a 403. An already-verified account
    gets sent=false without error.
    """
    if isinstance(user, MachineUser):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="API keys have no email address to verify")
    if user.email_verified:
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
@router.post("/forgot-password", responses=errors(429))
# Always returns the same thing, whether or not the address exists. Anything else turns this endpoint into a way to
# enumerate who has an account.
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: DbSession,
    _: None = Depends(rate_limit(limit=settings.rate_limit_forgot_password_per_minute)),
) -> dict[str, Any]:
    """Start a password reset for an email address.

    Always answers the same way whether or not the address has an account, so
    it cannot be used to enumerate emails. Rate-limited per IP.
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


@router.post("/reset-password", responses=errors(400, 429))
# On success every existing session for that user is revoked: if the reset was prompted by a compromise, the
# attacker's sessions must not survive it.
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    db: DbSession,
    _: None = Depends(rate_limit(limit=settings.rate_limit_forgot_password_per_minute)),
) -> dict[str, Any]:
    """Complete a password reset with the emailed token.

    On success every existing session for that user is revoked, so an attacker
    who triggered the reset cannot keep their sessions. The token is single-use;
    an invalid or expired one gets a 400. Rate-limited per IP.
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
        cast(Table, Session.__table__)
        .delete()
        .where(Session.__table__.c.user_id == user.id)
    )
    db.commit()
    audit(
        db, action="auth.password_reset", actor_user_id=user.id,
        organization_id=user.organization_id,
    )
    return {"reset": True, "message": "Password updated. Sign in with your new password."}


@router.post("/change-password", responses=errors(400, 401, 403))
# `unverified_user`: the password being changed is the one the customer was emailed about. Gating this behind
# verification would lock out exactly the person who cannot receive mail yet. It still requires the *current*
# password, so this is not a takeover vector.
def change_password(
    payload: ChangePasswordRequest,
    db: DbSession,
    user: Annotated[User | MachineUser, Depends(unverified_user)],
) -> dict[str, Any]:
    """Change the caller's own password; requires the current one.

    Works before email verification on purpose, so a customer who cannot
    receive mail is not locked out of their account. API keys get a 403.
    """
    if isinstance(user, MachineUser):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="API keys have no password to change")
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


@router.patch("/settings", responses=errors(401, 403))
# ``unverified_user`` on purpose (like change-password): these are personal preferences of your own account, not
# tenant data, and a freshly signed-up customer should be able to set them before verifying their address. The
# digest itself only ever goes out to verified addresses.
def update_settings(
    payload: UserSettingsUpdate,
    db: DbSession,
    user: Annotated[User | MachineUser, Depends(unverified_user)],
) -> dict[str, Any]:
    """Update the caller's own account preferences (the weekly email digest).

    Works before email verification on purpose; API keys get a 403. The digest
    itself only ever goes out to verified addresses.
    """
    if getattr(user, "is_machine", False):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="API keys have no account preferences")
    user = cast(User, user)  # the guard above excludes MachineUser; mypy cannot narrow through getattr
    if payload.weekly_digest_enabled is not None:
        user.weekly_digest_enabled = payload.weekly_digest_enabled
    db.commit()
    audit(
        db, action="auth.settings_updated", actor_user_id=user.id,
        organization_id=user.organization_id,
    )
    return _user_dict(user)


__all__ = ["router"]

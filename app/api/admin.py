"""Admin API: user management with last-admin protections."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from app.core.auth.api import DbSession, current_user, require_role
from app.core.auth.service import ROLE_LEVEL, audit, role_at_least
from app.core.database.identity import User

router = APIRouter(prefix="/admin", tags=["admin"])


class UserPatchRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None


def _require_admin(user=Depends(current_user)):
    if not role_at_least(getattr(user, "role", ""), "admin"):
        raise HTTPException(status_code=403, detail="Requires admin role")
    return user


@router.get("/users")
def list_users(
    search: str | None = None,
    limit: int = 50,
    db: DbSession = None,
    admin: Annotated[object, Depends(_require_admin)] = None,
) -> dict:
    query = select(User).order_by(User.id).limit(min(limit, 200))
    if search:
        query = query.where(User.email.ilike(f"%{search}%"))
    org = getattr(admin, "organization_id", None)
    # Machine principals (org=None) see all tenants; humans see their own.
    if org is not None:
        query = query.where(User.organization_id == org)
    rows = db.execute(query).scalars().all()
    return {
        "count": len(rows),
        "users": [
            {"id": u.id, "email": u.email, "role": u.role,
             "is_active": u.is_active, "organization_id": u.organization_id}
            for u in rows
        ],
    }


@router.patch("/users/{user_id}")
def patch_user(
    user_id: int,
    payload: UserPatchRequest,
    db: DbSession = None,
    admin: Annotated[object, Depends(_require_admin)] = None,
) -> dict:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    org = getattr(admin, "organization_id", None)
    if org is not None and target.organization_id != org:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.role is not None:
        if payload.role not in ROLE_LEVEL:
            raise HTTPException(status_code=400, detail=f"Unknown role '{payload.role}'")
        demoting_last_admin = (
            target.role == "admin"
            and not role_at_least(payload.role, "admin")
        )
        if demoting_last_admin:
            admins = db.execute(
                select(func.count(User.id))
                .where(User.organization_id == target.organization_id)
                .where(User.role == "admin")
                .where(User.is_active.is_(True))
            ).scalar() or 0
            if admins <= 1:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot demote the last active admin of an organization",
                )
        target.role = payload.role

    if payload.is_active is not None:
        deactivating_last_admin = (
            target.role == "admin" and target.is_active and not payload.is_active
        )
        if deactivating_last_admin:
            admins = db.execute(
                select(func.count(User.id))
                .where(User.organization_id == target.organization_id)
                .where(User.role == "admin")
                .where(User.is_active.is_(True))
            ).scalar() or 0
            if admins <= 1:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot deactivate the last active admin of an organization",
                )
        target.is_active = payload.is_active

    db.commit()
    audit(
        db, action="admin.user_patch",
        actor_user_id=getattr(admin, "id", None),
        organization_id=target.organization_id,
        resource=f"user:{target.id}",
    )
    return {
        "id": target.id, "email": target.email, "role": target.role,
        "is_active": target.is_active,
    }


__all__ = ["router"]

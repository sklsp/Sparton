"""Tenant context resolution (Ares foundation).

The organization is ALWAYS derived from authenticated identity — never from
client-supplied IDs. Cross-tenant access returns 404 (not 403) so responses
do not leak whether a resource exists in another tenant.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth.api import current_user
from app.core.database.base import get_db
from app.core.database.identity import User

DbSession = Annotated[Session, Depends(get_db)]


class TenantContext:
    """The authenticated principal's tenant scope for one request."""

    def __init__(self, user: User | None, organization_id: int | None) -> None:
        self.user = user
        self.organization_id = organization_id

    @property
    def is_machine(self) -> bool:
        return self.user is None  # X-API-Key machine principal

    def scoped(self, query: Any):
        """Apply the tenant filter to a query.

        Machine principals (shared API key) see all tenants: they are
        server-side infrastructure credentials, not end users.
        """
        if self.is_machine or self.organization_id is None:
            return query
        column = query.column_descriptions[0]["entity"].organization_id
        return query.where(column == self.organization_id)


def get_tenant(
    request: Request, db: DbSession, user: Annotated[User | None, Depends(current_user)]
) -> TenantContext:
    if user is None:
        # current_user raises 401 unless a valid machine key was presented;
        # reaching here means the machine path authenticated.
        return TenantContext(None, None)
    return TenantContext(user, user.organization_id)


def scoped_or_404(tenant: TenantContext, db: Session, model: type, resource_id: int):
    """Fetch one tenant-scoped row or raise 404 without existence leaks."""
    query = tenant.scoped(select(model).where(model.id == resource_id))
    row = db.execute(query).scalars().first()
    if row is None:
        # Deliberately generic: echoing the ID would let callers distinguish
        # "exists in another tenant" from "does not exist".
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        )
    return row


__all__ = ["DbSession", "TenantContext", "get_tenant", "scoped_or_404"]

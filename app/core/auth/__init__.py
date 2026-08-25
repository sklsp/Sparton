"""Authentication, sessions, RBAC, and FastAPI auth dependencies."""

from app.core.auth.api import MachineUser, current_user, require_role
from app.core.auth.service import (
    Role,
    audit,
    create_session,
    ensure_default_organization,
    hash_password,
    resolve_session,
    revoke_session,
    role_at_least,
    verify_password,
)

__all__ = [
    "MachineUser",
    "Role",
    "audit",
    "create_session",
    "current_user",
    "ensure_default_organization",
    "hash_password",
    "require_role",
    "resolve_session",
    "revoke_session",
    "role_at_least",
    "verify_password",
]

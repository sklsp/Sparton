"""Users, organizations, sessions, projects, and audit logging.

Adapted from Ares `db/identity.py` with the SPARTON project system added:
projects are the central cross-domain workspace entity.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base, JSONType
from app.core.database.models import utcnow


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Display name. NOT unique: on a public signup form a global unique
    # constraint lets one user squat "Acme" and deny it to every other shop
    # (docs/DECISIONS.md D-009). The unique key is `id` (and `slug`).
    name: Mapped[str] = mapped_column(String(160), index=True)
    # URL-safe, unique, derived from `name` with a numeric suffix on collision.
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="organization")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # admin | manager | analyst | viewer
    role: Mapped[str] = mapped_column(String(20), default="viewer", index=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    organization: Mapped[Organization] = relationship(back_populates="users")
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")

    @property
    def is_valid(self) -> bool:
        if self.revoked_at is not None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            from datetime import timezone

            expires = expires.replace(tzinfo=timezone.utc)
        return expires > utcnow()


class Project(Base):
    """The central workspace entity. Documents, research, stores, datasets,
    training runs, workflows and generated assets all link to a project."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_project_org_name"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    resource: Mapped[str] = mapped_column(String(160), default="")
    outcome: Mapped[str] = mapped_column(String(20), default="success")
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


__all__ = ["AuditLog", "Organization", "Project", "Session", "User"]

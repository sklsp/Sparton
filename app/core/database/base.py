"""Database engine, session factory and declarative base (Ares foundation)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# JSONB on PostgreSQL, plain JSON everywhere else (SQLite in tests).
JSONType = JSON().with_variant(JSONB, "postgresql")

_connect_args: dict[str, Any] = {}
if settings.is_sqlite:
    # The agent runs on background threads and shares the engine pool.
    _connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

if settings.is_sqlite:

    @event.listens_for(Engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
        """SQLite ignores FK constraints unless asked, which would silently
        skip our ON DELETE CASCADE rules. PostgreSQL enforces them natively."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

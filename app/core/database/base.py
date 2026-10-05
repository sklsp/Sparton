"""Database engine, session factory and declarative base (Ares foundation)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Numeric, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# JSONB on PostgreSQL, plain JSON everywhere else (SQLite in tests).
JSONType = JSON().with_variant(JSONB, "postgresql")

#: Money. `Numeric(12, 4)` is a fixed-point decimal in the database, and
#: `asdecimal=True` makes SQLAlchemy hand it to Python as `Decimal` rather than
#: `float`.
#:
#: Why it matters for this product: the whole claim is that we report a
#: competitor's price exactly. A float cannot hold 0.45, so a Float column turns
#: "exact" into "exact to about fourteen decimal places" -- and the error shows
#: up in a customer-facing number and in a price-change comparison, which is
#: precisely where being wrong is expensive.
#:
#: SQLite has no real DECIMAL: it stores a float and converts on the way out, so
#: `asdecimal` is what actually makes the value a Decimal. PostgreSQL does the
#: arithmetic in fixed point, which is the point of doing this.
#:
#: Four decimal places is two more than any real price needs, and it matches
#: what the feeds publish, so nothing is lost in the round trip.
MONEY = Numeric(12, 4, asdecimal=True)

_connect_args: dict[str, Any] = {}
if settings.is_sqlite:
    # The agent runs on background threads and shares the engine pool.
    _connect_args["check_same_thread"] = False

def _json_default(value):
    """Teach JSON columns about Decimal.

    Prices are Decimal (see MONEY) and they end up inside JSON columns -- a
    change event's `detail` blob, a report's `facts`. The stdlib encoder has
    no idea what a Decimal is and raises, so the first price to be written took
    the whole report down with it.

    Serialising as a *string* rather than a float is the point: 0.45 would
    survive a float round trip as 0.45000000000000001, which is the error this
    column type exists to remove. The typed columns keep the real value, so the
    string is a faithful copy, not a lossy one.
    """
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


engine = create_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,
    connect_args=_connect_args,
    # Applies to every JSON column on this engine, so a Decimal cannot reach
    # one unhandled wherever it is put.
    json_serializer=lambda obj: json.dumps(obj, default=_json_default),
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

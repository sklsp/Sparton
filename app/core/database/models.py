"""Shared model helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; normalise before arithmetic."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)

"""Per-organization LLM token accounting.

Every provider call writes one row here. Two reasons this is a table and not a
log line:

1. **Billing.** Plan limits are expressed in tokens per day
   (docs/DECISIONS.md D-007, D-013). Retro-fitting the numbers after launch
   would need a backfill nobody can do.
2. **Cost.** ``docs/LAUNCH.md`` quotes a cost per customer per month; that
   number is only credible if it comes from measured usage.

``task`` is a free-form label ("report", "agent", "extract") so a single
organization's spend can be attributed to a feature.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, JSONType
from app.core.database.models import utcnow


class LLMUsage(Base):
    """One LLM request. Append-only; never updated."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable: calls made by the embedded worker, health probes, or a CLI
    # script have no authenticated user and therefore no organization.
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    provider: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(120), index=True)
    #: "agent" | "report" | "extract" | "chat" | ...
    task: Mapped[str] = mapped_column(String(40), default="generic", index=True)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)

    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    #: Populated when the provider reports a price for the model we used.
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)


#: Daily rollup and per-task attribution both scan (org, created_at).
Index("ix_llm_usage_org_created", LLMUsage.organization_id, LLMUsage.created_at)


def tokens_used_today(db, organization_id: int | None) -> int:
    """Total tokens for one organization since midnight UTC.

    Counts *all* tasks: a customer's plan limit is on their total consumption,
    not on which feature happened to spend it.
    """
    if organization_id is None:
        return 0
    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    total = db.execute(
        select(func.coalesce(func.sum(LLMUsage.total_tokens), 0)).where(
            LLMUsage.organization_id == organization_id,
            LLMUsage.created_at >= start_of_day,
        )
    ).scalar()
    return int(total or 0)


def usage_summary(db, organization_id: int, *, days: int = 30) -> dict[str, Any]:
    """Per-task and per-model totals for the last `days` days."""
    since = utcnow() - timedelta(days=days)
    scoped = (
        LLMUsage.organization_id == organization_id,
        LLMUsage.created_at >= since,
    )

    by_task = {
        task: int(tokens)
        for task, tokens in db.execute(
            select(LLMUsage.task, func.coalesce(func.sum(LLMUsage.total_tokens), 0))
            .where(*scoped)
            .group_by(LLMUsage.task)
        ).all()
    }
    by_model = {
        model: {"tokens": int(tokens), "cost_usd": round(float(cost or 0.0), 6)}
        for model, tokens, cost in db.execute(
            select(
                LLMUsage.model,
                func.coalesce(func.sum(LLMUsage.total_tokens), 0),
                func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
            )
            .where(*scoped)
            .group_by(LLMUsage.model)
        ).all()
    }
    total_tokens = db.execute(
        select(func.coalesce(func.sum(LLMUsage.total_tokens), 0)).where(*scoped)
    ).scalar()
    total_cost = db.execute(
        select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(*scoped)
    ).scalar()
    requests = db.execute(select(func.count(LLMUsage.id)).where(*scoped)).scalar()

    return {
        "days": days,
        "requests": int(requests or 0),
        "total_tokens": int(total_tokens or 0),
        "estimated_cost_usd": round(float(total_cost or 0.0), 4),
        "by_task": by_task,
        "by_model": by_model,
    }


__all__ = ["LLMUsage", "tokens_used_today", "usage_summary"]

"""Tests for app/core/database/usage_models.py: per-organization LLM accounting.

Covers the branches the billing suite only touches indirectly: a missing
organization, and the whole usage_summary window (grouping by task and model,
rows outside the window, an organization with no usage at all).
"""

from __future__ import annotations

from datetime import timedelta

from app.core.database.identity import Organization
from app.core.database.models import utcnow
from app.core.database.usage_models import LLMUsage, tokens_used_today, usage_summary


def _make_org(db_session, email_local: str) -> Organization:
    org = Organization(name=f"Usage Test {email_local}", slug=email_local)
    db_session.add(org)
    db_session.flush()
    return org


def _add_usage(
    db_session,
    organization_id: int | None,
    *,
    task="report",
    model="m-a",
    total=100,
    cost=0.0,
    created_at=None,
) -> LLMUsage:
    row = LLMUsage(
        organization_id=organization_id,
        provider="test",
        model=model,
        task=task,
        prompt_tokens=total // 2,
        completion_tokens=total - total // 2,
        total_tokens=total,
        cost_usd=cost,
        created_at=created_at if created_at is not None else utcnow(),
    )
    db_session.add(row)
    db_session.commit()
    return row


class TestTokensUsedToday:
    def test_returns_zero_for_a_missing_organization(self, db_session):
        """Worker and probe calls have no organization at all."""
        assert tokens_used_today(db_session, None) == 0

    def test_counts_only_this_organizations_rows_since_midnight(self, db_session):
        org = _make_org(db_session, "today")
        other = _make_org(db_session, "other")
        _add_usage(db_session, org.id, total=1234)
        # Another tenant's spend must never leak into this one.
        _add_usage(db_session, other.id, total=9999)
        # Yesterday's spend does not count against today.
        _add_usage(
            db_session, org.id, total=5000,
            created_at=utcnow() - timedelta(days=1),
        )
        assert tokens_used_today(db_session, org.id) == 1234


class TestUsageSummary:
    def test_groups_by_task_and_model(self, db_session):
        """The summary is the per-task and per-model split of one window."""
        org = _make_org(db_session, "groups")
        _add_usage(db_session, org.id, task="report", model="m-a", total=100, cost=0.5)
        _add_usage(db_session, org.id, task="report", model="m-b", total=200, cost=0.25)
        _add_usage(db_session, org.id, task="agent", model="m-a", total=300, cost=1.75)

        summary = usage_summary(db_session, org.id)
        assert summary["days"] == 30
        assert summary["requests"] == 3
        assert summary["total_tokens"] == 600
        assert summary["estimated_cost_usd"] == 2.5
        assert summary["by_task"] == {"report": 300, "agent": 300}
        assert summary["by_model"] == {
            "m-a": {"tokens": 400, "cost_usd": 2.25},
            "m-b": {"tokens": 200, "cost_usd": 0.25},
        }

    def test_excludes_rows_outside_the_window(self, db_session):
        """The window is the last `days` days; older rows are not counted."""
        org = _make_org(db_session, "window")
        old_total = 700
        recent_total = 300
        _add_usage(
            db_session, org.id, total=old_total, cost=1.0,
            created_at=utcnow() - timedelta(days=40),
        )
        _add_usage(db_session, org.id, total=recent_total, cost=0.5)

        default = usage_summary(db_session, org.id)  # days=30
        assert default["requests"] == 1
        assert default["total_tokens"] == recent_total
        assert default["estimated_cost_usd"] == 0.5

        wide = usage_summary(db_session, org.id, days=60)
        assert wide["days"] == 60
        assert wide["requests"] == 2
        assert wide["total_tokens"] == old_total + recent_total
        assert wide["estimated_cost_usd"] == 1.5

    def test_an_organization_with_no_usage_gets_zeroes(self, db_session):
        org = _make_org(db_session, "empty")
        summary = usage_summary(db_session, org.id)
        assert summary == {
            "days": 30,
            "requests": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "by_task": {},
            "by_model": {},
        }

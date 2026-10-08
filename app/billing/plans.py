"""Plan definitions and server-side enforcement.

The single source of truth for what a customer may do. Every mutating product
route calls a `check_*` function before it does anything, so the browser is
never the thing that decides whether an action is allowed (D-012).

Enforcement is deliberately **server-side and explicit** rather than relying on
the existing `TenantContext.scoped`, because that helper returns *unscoped*
queries for machine principals — correct for the ops API, wrong for a billing
check. Billing code therefore always filters on an explicit
``organization_id`` and refuses to proceed without one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database.ecommerce_models import Competitor, Shop
from app.core.database.usage_models import tokens_used_today

#: Plan identifiers, also used as the Stripe price lookup key.
FREE = "free"
PRO = "pro"
BUSINESS = "business"

TRIAL_DAYS = 14


@dataclass(frozen=True, slots=True)
class Plan:
    """One plan. `price_cents` is what Stripe charges per month."""

    id: str
    name: str
    price_cents: int
    #: The env var holding this plan's Stripe price id, or "" for the free plan.
    price_env: str
    shops: int
    competitors: int
    #: Daily LLM token allowance. A weekly report is ~8k tokens, so 2,000 is
    #: generous for a week of reports and not enough to be a free API for
    #: someone else's benefit.
    daily_tokens: int
    crawl_frequency_hours: int
    #: Months of report history kept and browsable.
    report_history_months: int
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def price_label(self) -> str:
        if self.price_cents == 0:
            return "Free"
        return f"€{self.price_cents / 100:.0f}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "price_cents": self.price_cents,
            "price_label": self.price_label,
            "shops": self.shops,
            "competitors": self.competitors,
            "daily_tokens": self.daily_tokens,
            "crawl_frequency_hours": self.crawl_frequency_hours,
            "report_history_months": self.report_history_months,
            "features": self.features,
        }


PLANS: dict[str, Plan] = {
    FREE: Plan(
        id=FREE, name="Free", price_cents=0, price_env="",
        shops=1, competitors=3, daily_tokens=2_000,
        crawl_frequency_hours=168, report_history_months=1,
        features={
            "weekly_report": True, "price_alerts": True,
            "assortment_alerts": True, "email_digest": False,
            "api_access": False, "priority_support": False,
        },
    ),
    PRO: Plan(
        id=PRO, name="Pro", price_cents=2_900, price_env="STRIPE_PRICE_PRO",
        shops=3, competitors=15, daily_tokens=20_000,
        crawl_frequency_hours=24, report_history_months=12,
        features={
            "weekly_report": True, "price_alerts": True,
            "assortment_alerts": True, "email_digest": True,
            "api_access": False, "priority_support": False,
        },
    ),
    BUSINESS: Plan(
        id=BUSINESS, name="Business", price_cents=7_900,
        price_env="STRIPE_PRICE_BUSINESS",
        shops=10, competitors=50, daily_tokens=100_000,
        crawl_frequency_hours=12, report_history_months=36,
        features={
            "weekly_report": True, "price_alerts": True,
            "assortment_alerts": True, "email_digest": True,
            "api_access": True, "priority_support": True,
        },
    ),
}

#: Display order for the pricing table.
PLAN_ORDER = (FREE, PRO, BUSINESS)

#: New organizations always start here. A card is required to go higher, which
#: keeps the signup path free of payment risk.
DEFAULT_PLAN = FREE


def get_plan(plan_id: str | None) -> Plan:
    """Unknown or missing plan falls back to Free.

    Failing *closed* matters: if a customer's plan id is unreadable for any
    reason they get the smallest allowance, never an accidental unlimited one.
    """
    return PLANS.get((plan_id or "").lower(), PLANS[FREE])


def all_plans() -> list[dict[str, Any]]:
    return [PLANS[p].to_dict() for p in PLAN_ORDER]


def counts(db: Session, organization_id: int | None) -> dict[str, int]:
    """Current usage for one organization.

    Always filtered on an explicit organization_id: a billing check must never
    see another tenant's counts, and must never see *all* tenants' counts.
    """
    if organization_id is None:
        return {"shops": 0, "competitors": 0, "daily_tokens": 0}
    shops = db.execute(
        select(func.count(Shop.id)).where(Shop.organization_id == organization_id)
    ).scalar()
    competitors = db.execute(
        select(func.count(Competitor.id)).where(
            Competitor.organization_id == organization_id
        )
    ).scalar()
    return {
        "shops": int(shops or 0),
        "competitors": int(competitors or 0),
        "daily_tokens": tokens_used_today(db, organization_id),
    }


def usage_report(db: Session, organization_id: int | None, plan_id: str | None) -> dict[str, Any]:
    """What the dashboard's usage panel and the API both return."""
    plan = get_plan(plan_id)
    used = counts(db, organization_id)
    return {
        "plan": plan.to_dict(),
        "used": used,
        "remaining": {
            "shops": max(0, plan.shops - used["shops"]),
            "competitors": max(0, plan.competitors - used["competitors"]),
            "daily_tokens": max(0, plan.daily_tokens - used["daily_tokens"]),
        },
    }


def _reject(message: str) -> HTTPException:
    """402 Payment Required: the request is valid, the *plan* is not enough.

    402 rather than 403 — the customer is authenticated and authorised, they
    just need to pay. The machine-readable `error` field lets the UI show the
    right upgrade prompt without parsing prose.
    """
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={"error": "plan_limit_reached", "message": message, "upgrade": True},
    )


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def check_shops(db: Session, organization_id: int | None, plan_id: str | None) -> None:
    plan = get_plan(plan_id)
    if counts(db, organization_id)["shops"] >= plan.shops:
        raise _reject(
            f"Your {plan.name} plan includes {plan.shops} shop{_plural(plan.shops)}. "
            f"Upgrade to add another."
        )


def check_competitors(db: Session, organization_id: int | None, plan_id: str | None) -> None:
    plan = get_plan(plan_id)
    if counts(db, organization_id)["competitors"] >= plan.competitors:
        raise _reject(
            f"Your {plan.name} plan includes {plan.competitors} "
            f"competitor{_plural(plan.competitors)}. Upgrade to track more."
        )


def check_tokens(db: Session, organization_id: int | None, plan_id: str | None) -> None:
    """Guard the daily LLM allowance.

    Checked *before* the report job is queued, not inside it, so the customer
    gets an immediate, actionable error instead of a job that fails anonymously
    thirty seconds later.
    """
    plan = get_plan(plan_id)
    if tokens_used_today(db, organization_id) >= plan.daily_tokens:
        raise _reject(
            f"You have used today's AI allowance ({plan.daily_tokens:,} tokens). "
            f"It resets at midnight UTC, or upgrade for a higher limit."
        )


def check_crawl_frequency(plan_id: str | None, hours: int | None) -> int:
    """Clamp a requested cadence to what the plan allows.

    The plan sets the fastest cadence. Clamps rather than rejects: a request
    faster than the plan is valid, we simply do it less often. A slower request
    is kept, and no request means the plan's own cadence.
    """
    plan = get_plan(plan_id)
    if not hours:
        return plan.crawl_frequency_hours
    return min(max(6, int(hours), plan.crawl_frequency_hours), 24 * 30)


__all__ = [
    "BUSINESS",
    "DEFAULT_PLAN",
    "FREE",
    "PLANS",
    "PRO",
    "TRIAL_DAYS",
    "Plan",
    "all_plans",
    "check_competitors",
    "check_crawl_frequency",
    "check_shops",
    "check_tokens",
    "counts",
    "get_plan",
    "usage_report",
]

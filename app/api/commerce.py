"""Sparton Intelligence — the customer-facing API.

    POST   /shops                     add your shop URL
    GET    /shops                     list your shops
    GET    /shops/{id}                one shop with its competitors
    PATCH  /shops/{id}                change cadence, name, active flag
    DELETE /shops/{id}                stop tracking a shop
    POST   /shops/{id}/discover       propose competitors (nothing is stored)
    POST   /shops/{id}/crawl          crawl now
    POST   /shops/{id}/report         write a report now
    GET    /shops/{id}/reports        past reports

    GET    /competitors               your watchlist
    POST   /competitors               add a competitor by URL
    DELETE /competitors/{id}          stop watching one
    POST   /competitors/{id}/crawl    crawl one now

    GET    /changes                   the in-app alert inbox
    POST   /changes/{id}/ack          mark one as read
    GET    /reports                   all reports
    GET    /reports/{id}              one report

Every handler resolves its tenant from the authenticated principal and filters
on it explicitly. Plan limits (Phase 4) are enforced server-side at each
mutating route — the browser never decides what a customer may do.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.billing.plans import check_competitors, check_crawl_frequency, check_shops, check_tokens
from app.billing.service import effective_plan
from app.core.auth.api import DbSession, current_user
from app.core.database.ecommerce_models import ChangeEvent, Competitor, Report, Shop
from app.core.database.models import utcnow
from app.core.jobs.queue import enqueue
from app.ecommerce.discovery import (
    ShopError,
    add_competitor,
    add_shop,
    shop_already_tracked,
    suggest_competitors,
)
from app.ecommerce.urls import InvalidShopUrl, normalize_url
from app.ecommerce.feeds import source_label
from app.ecommerce.reports import report_to_dict

router = APIRouter(tags=["intelligence"])


class ShopCreate(BaseModel):
    url: str = Field(min_length=3, max_length=500)
    name: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=120)
    currency: str = Field(default="EUR", max_length=8)
    crawl_frequency_hours: int = Field(default=168, ge=6, le=720)


class ShopUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    crawl_frequency_hours: int | None = Field(default=None, ge=6, le=720)
    is_active: bool | None = None


class CompetitorCreate(BaseModel):
    url: str = Field(min_length=3, max_length=500)
    name: str = Field(default="", max_length=200)
    shop_id: int | None = None


def _org_id(user) -> int | None:
    return getattr(user, "organization_id", None)


def _plan_of(db, user) -> str:
    """The caller's plan, resolved server-side on every mutating route.

    Never read from the request body or a header: the browser does not get to
    decide what plan it is on (D-012).
    """
    return effective_plan(db, _org_id(user))


def _fail(exc: ShopError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _shop_dict(shop: Shop, *, competitor_count: int = 0) -> dict[str, Any]:
    return {
        "id": shop.id,
        "name": shop.name,
        "url": shop.url,
        "domain": shop.domain,
        "platform": shop.platform,
        "category": shop.category,
        "currency": shop.currency,
        "crawl_frequency_hours": shop.crawl_frequency_hours,
        "is_active": shop.is_active,
        "competitor_count": competitor_count,
        "last_crawled_at": shop.last_crawled_at.isoformat() if shop.last_crawled_at else None,
        "next_crawl_at": shop.next_crawl_at.isoformat() if shop.next_crawl_at else None,
        "last_status": shop.last_status,
        "created_at": shop.created_at.isoformat() if shop.created_at else None,
    }


def _competitor_dict(row: Competitor) -> dict[str, Any]:
    return {
        "id": row.id,
        "shop_id": row.shop_id,
        "domain": row.domain,
        "name": row.name,
        "url": row.url,
        "platform": row.platform,
        "discovery_method": row.discovery_method,
        "confidence": row.confidence,
        "is_active": row.is_active,
        "product_count": row.product_count,
        "last_crawled_at": row.last_crawled_at.isoformat() if row.last_crawled_at else None,
        "last_status": row.last_status,
        # Which tier the last crawl used, and the wording to show for it. The
        # UI renders `source_label`; `data_source` is there so a client can
        # branch on it without parsing English.
        "data_source": row.last_source or "html",
        "source_label": source_label(row.last_source or "html"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _change_dict(row: ChangeEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "shop_id": row.shop_id,
        "kind": row.kind,
        "severity": row.severity,
        "title": row.title,
        "summary": row.summary,
        "competitor": row.competitor_name or row.competitor_domain,
        "competitor_domain": row.competitor_domain,
        "product": row.product_name,
        # Decimals become JSON numbers, not strings: the dashboard does
        # arithmetic on these to render "down €4.20", and a quoted number
        # would need parsing at every use. Pydantic's JSON encoder already
        # handles Decimal, so this is a pass-through.
        "previous_price": row.previous_price,
        "new_price": row.new_price,
        "currency": row.currency,
        "delta": row.delta,
        "delta_pct": row.delta_pct,
        # The evidence link: the exact page the number was read from.
        "evidence_url": row.evidence_url,
        "detected_at": row.detected_at.isoformat() if row.detected_at else None,
        "acknowledged_at": (
            row.acknowledged_at.isoformat() if row.acknowledged_at else None
        ),
    }


def get_shop_or_404(db, shop_id: int, organization_id: int | None) -> Shop:
    """Tenant-scoped fetch.

    404 rather than 403, so we never confirm that another customer's shop
    exists.
    """
    row = db.execute(
        select(Shop).where(Shop.id == shop_id, Shop.organization_id == organization_id)
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Shop not found")
    return row


def get_competitor_or_404(db, competitor_id: int, organization_id: int | None) -> Competitor:
    row = db.execute(
        select(Competitor).where(
            Competitor.id == competitor_id, Competitor.organization_id == organization_id
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Competitor not found")
    return row


# ---------------------------------------------------------------------------
# Shops
# ---------------------------------------------------------------------------
@router.post("/shops", status_code=201)
def create_shop(
    payload: ShopCreate,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """Add your shop. This is the first step of the whole product."""
    org = _org_id(user)
    # Validate and de-duplicate *before* the plan check: a customer re-adding a
    # shop they already have should be told "you already added it", not told to
    # upgrade. The 402 must be reserved for "you want more than your plan has".
    try:
        normalized = normalize_url(payload.url)
    except InvalidShopUrl as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if shop_already_tracked(db, org, normalized.domain):
        raise HTTPException(status_code=409, detail="You have already added this shop")

    check_shops(db, org, _plan_of(db, user))
    try:
        shop = add_shop(
            db,
            org,
            url=payload.url,
            name=payload.name,
            category=payload.category,
            currency=payload.currency,
            crawl_frequency_hours=check_crawl_frequency(
                _plan_of(db, user), payload.crawl_frequency_hours
            ),
        )
    except ShopError as exc:
        raise _fail(exc) from exc
    return _shop_dict(shop)


@router.get("/shops")
def list_shops(
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org = _org_id(user)
    rows = db.execute(
        select(Shop).where(Shop.organization_id == org).order_by(Shop.created_at.desc())
    ).scalars().all()
    counts = {
        shop_id: count
        for shop_id, count in db.execute(
            select(Competitor.shop_id, __import__("sqlalchemy").func.count(Competitor.id))
            .where(Competitor.organization_id == org)
            .group_by(Competitor.shop_id)
        ).all()
    }
    return {
        "count": len(rows),
        "shops": [_shop_dict(s, competitor_count=counts.get(s.id, 0)) for s in rows],
    }


@router.get("/shops/{shop_id}")
def get_shop(
    shop_id: int,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org = _org_id(user)
    shop = get_shop_or_404(db, shop_id, org)
    competitors = db.execute(
        select(Competitor)
        .where(Competitor.organization_id == org, Competitor.shop_id == shop_id)
        .order_by(Competitor.created_at)
    ).scalars().all()
    payload = _shop_dict(shop, competitor_count=len(competitors))
    payload["competitors"] = [_competitor_dict(c) for c in competitors]
    return payload


@router.patch("/shops/{shop_id}")
def update_shop(
    shop_id: int,
    payload: ShopUpdate,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    shop = get_shop_or_404(db, shop_id, _org_id(user))
    if payload.name is not None:
        shop.name = payload.name.strip()[:200]
    if payload.category is not None:
        shop.category = payload.category.strip()[:120]
    if payload.crawl_frequency_hours is not None:
        # The plan clamps the cadence rather than refusing it: the customer
        # asked for something valid, we just do it less often.
        shop.crawl_frequency_hours = check_crawl_frequency(
            _plan_of(db, user), payload.crawl_frequency_hours
        )
    if payload.is_active is not None:
        shop.is_active = payload.is_active
    db.commit()
    db.refresh(shop)
    return _shop_dict(shop)


@router.delete("/shops/{shop_id}")
def delete_shop(
    shop_id: int,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    shop = get_shop_or_404(db, shop_id, _org_id(user))
    db.delete(shop)  # cascades to competitors, captures, changes
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Competitors
# ---------------------------------------------------------------------------
@router.get("/competitors")
def list_competitors(
    shop_id: int | None = None,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org = _org_id(user)
    query = select(Competitor).where(Competitor.organization_id == org)
    if shop_id is not None:
        query = query.where(Competitor.shop_id == shop_id)
    rows = db.execute(query.order_by(Competitor.created_at.desc())).scalars().all()
    return {"count": len(rows), "competitors": [_competitor_dict(c) for c in rows]}


@router.post("/competitors", status_code=201)
def create_competitor(
    payload: CompetitorCreate,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org = _org_id(user)
    check_competitors(db, org, _plan_of(db, user))
    try:
        competitor = add_competitor(
            db,
            org,
            url=payload.url,
            name=payload.name,
            shop_id=payload.shop_id,
        )
    except ShopError as exc:
        raise _fail(exc) from exc
    return _competitor_dict(competitor)


@router.delete("/competitors/{competitor_id}")
def delete_competitor(
    competitor_id: int,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    competitor = get_competitor_or_404(db, competitor_id, _org_id(user))
    db.delete(competitor)  # cascades to captures and changes
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@router.post("/shops/{shop_id}/discover")
def discover_competitors(
    shop_id: int,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """Propose competitors. **Nothing is stored** until the customer confirms.

    Auto-adding would mean starting to crawl a stranger's server on the strength
    of a search result, which is both impolite and a liability.
    """
    shop = get_shop_or_404(db, shop_id, _org_id(user))
    suggestions = suggest_competitors(db, _org_id(user), shop)
    return {
        "count": len(suggestions),
        "suggestions": suggestions,
        "message": (
            "Review these and add the ones you actually compete with. "
            "We will not crawl anything until you do."
        ),
    }


# ---------------------------------------------------------------------------
# Crawl + report triggers
# ---------------------------------------------------------------------------
@router.post("/shops/{shop_id}/crawl", status_code=202)
def crawl_shop_now(
    shop_id: int,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """Queue an immediate crawl of every competitor on this shop."""
    org = _org_id(user)
    shop = get_shop_or_404(db, shop_id, org)
    competitors = db.execute(
        select(Competitor).where(
            Competitor.organization_id == org,
            Competitor.shop_id == shop.id,
            Competitor.is_active.is_(True),
        )
    ).scalars().all()
    if not competitors:
        raise HTTPException(
            status_code=400,
            detail="Add at least one competitor before crawling",
        )
    job, created = enqueue(
        db,
        type="crawl_shop",
        payload={"shop_id": shop.id, "competitor_ids": [c.id for c in competitors]},
        organization_id=org,
        # Per-day key: a manual re-crawl and the scheduler cannot both run on
        # the same day (docs/DECISIONS.md D-015).
        idempotency_key=f"crawl:{shop.id}:{utcnow():%Y-%m-%d}",
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "created": created,
        "competitors": len(competitors),
        "message": (
            f"Crawling {len(competitors)} competitor(s). "
            "Changes will appear in your alerts when it finishes."
        ),
    }


@router.post("/competitors/{competitor_id}/crawl", status_code=202)
def crawl_competitor_now(
    competitor_id: int,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org = _org_id(user)
    competitor = get_competitor_or_404(db, competitor_id, org)
    job, created = enqueue(
        db,
        type="crawl_competitor",
        payload={"competitor_id": competitor.id, "shop_id": competitor.shop_id},
        organization_id=org,
        idempotency_key=f"crawl1:{competitor.id}:{utcnow():%Y-%m-%d-%H}",
    )
    return {"job_id": job.id, "status": job.status, "created": created}


@router.post("/shops/{shop_id}/report", status_code=202)
def generate_report_now(
    shop_id: int,
    days: int = Query(default=7, ge=1, le=90),
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """Write a report now over the last `days` days."""
    org = _org_id(user)
    shop = get_shop_or_404(db, shop_id, org)
    # The daily AI allowance is checked *here*, before the job is queued, so the
    # customer gets an immediate error instead of a job that fails anonymously
    # thirty seconds later.
    check_tokens(db, org, _plan_of(db, user))
    job, created = enqueue(
        db,
        type="generate_report",
        payload={"shop_id": shop.id, "days": days, "kind": "manual"},
        organization_id=org,
        idempotency_key=f"report:{shop.id}:{days}:{utcnow():%Y-%m-%d-%H}",
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "created": created,
        "message": "Your report is being written. It will appear in Reports.",
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
@router.get("/changes")
def list_changes(
    shop_id: int | None = None,
    kind: str | None = None,
    severity: str | None = None,
    unacknowledged_only: bool = False,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=200),
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """The in-app alert inbox, newest first. Every row carries an evidence link."""
    from datetime import timedelta

    org = _org_id(user)
    since = utcnow() - timedelta(days=days)
    query = select(ChangeEvent).where(
        ChangeEvent.organization_id == org, ChangeEvent.detected_at >= since
    )
    if shop_id is not None:
        query = query.where(ChangeEvent.shop_id == shop_id)
    if kind:
        query = query.where(ChangeEvent.kind == kind)
    if severity:
        query = query.where(ChangeEvent.severity == severity)
    if unacknowledged_only:
        query = query.where(ChangeEvent.acknowledged_at.is_(None))

    rows = db.execute(
        query.order_by(ChangeEvent.detected_at.desc()).limit(limit)
    ).scalars().all()
    unacked = db.execute(
        select(ChangeEvent).where(
            ChangeEvent.organization_id == org, ChangeEvent.acknowledged_at.is_(None)
        )
    ).scalars().all()
    return {
        "count": len(rows),
        "unacknowledged": len(unacked),
        "changes": [_change_dict(c) for c in rows],
    }


@router.post("/changes/{change_id}/ack")
def acknowledge_change(
    change_id: int,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org = _org_id(user)
    row = db.execute(
        select(ChangeEvent).where(
            ChangeEvent.id == change_id, ChangeEvent.organization_id == org
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Change not found")
    if row.acknowledged_at is None:
        row.acknowledged_at = utcnow()
        db.commit()
    return {"id": row.id, "acknowledged_at": row.acknowledged_at.isoformat()}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@router.get("/shops/{shop_id}/reports")
def list_shop_reports(
    shop_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org = _org_id(user)
    get_shop_or_404(db, shop_id, org)
    rows = db.execute(
        select(Report)
        .where(Report.organization_id == org, Report.shop_id == shop_id)
        .order_by(Report.created_at.desc())
        .limit(limit)
    ).scalars().all()
    # Omit the markdown in listings; the detail route serves it.
    return {
        "count": len(rows),
        "reports": [report_to_dict(r, include_markdown=False) for r in rows],
    }


@router.get("/reports")
def list_reports(
    limit: int = Query(default=20, ge=1, le=100),
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org = _org_id(user)
    rows = db.execute(
        select(Report)
        .where(Report.organization_id == org)
        .order_by(Report.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "count": len(rows),
        "reports": [report_to_dict(r, include_markdown=False) for r in rows],
    }


@router.get("/reports/{report_id}")
def get_report(
    report_id: int,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org = _org_id(user)
    row = db.execute(
        select(Report).where(Report.id == report_id, Report.organization_id == org)
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report_to_dict(row)


# ---------------------------------------------------------------------------
# Overview — the dashboard's single call
# ---------------------------------------------------------------------------
@router.get("/overview")
def overview(
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """Everything the dashboard needs in one round trip.

    One request instead of five keeps the first paint to a single round trip,
    which matters on the phone connections this product is used from.
    """
    from datetime import timedelta

    org = _org_id(user)
    week_ago = utcnow() - timedelta(days=7)

    shops = db.execute(
        select(Shop).where(Shop.organization_id == org).order_by(Shop.created_at)
    ).scalars().all()
    competitors = db.execute(
        select(Competitor).where(Competitor.organization_id == org)
    ).scalars().all()
    recent = db.execute(
        select(ChangeEvent)
        .where(
            ChangeEvent.organization_id == org, ChangeEvent.detected_at >= week_ago
        )
        .order_by(ChangeEvent.detected_at.desc())
        .limit(25)
    ).scalars().all()
    unacked = db.execute(
        select(ChangeEvent).where(
            ChangeEvent.organization_id == org, ChangeEvent.acknowledged_at.is_(None)
        )
    ).scalars().all()
    latest_report = db.execute(
        select(Report)
        .where(Report.organization_id == org)
        .order_by(Report.created_at.desc())
        .limit(1)
    ).scalars().first()

    return {
        "shops": [_shop_dict(s) for s in shops],
        "competitor_count": len(competitors),
        "competitors_healthy": sum(1 for c in competitors if c.last_status == "COMPLETED"),
        "competitors_failing": sum(
            1 for c in competitors if c.last_status in ("FAILED", "BLOCKED")
        ),
        "changes_this_week": len(recent),
        "unacknowledged": len(unacked),
        "recent_changes": [_change_dict(c) for c in recent[:10]],
        "latest_report": (
            report_to_dict(latest_report, include_markdown=False) if latest_report else None
        ),
    }


__all__ = ["router"]

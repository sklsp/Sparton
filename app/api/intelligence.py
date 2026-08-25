"""Intelligence API: research jobs, external stores, opportunities."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from app.api.schemas import ResearchJobRequest
from app.core.auth.api import DbSession, current_user
from app.core.database.base import SessionLocal
from app.core.database.domain_models import ExternalProduct, ExternalStore, Job, Opportunity
from app.core.jobs.queue import enqueue
from app.core.observability.metrics import inc
from app.research.intelligence import get_opportunity, list_opportunities

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


def _org_id(user) -> int | None:
    return getattr(user, "organization_id", None)


@router.post("/jobs", status_code=202)
def start_research(
    payload: ResearchJobRequest,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    inc("research_jobs_total")
    job, created = enqueue(
        db,
        type="research",
        payload={"query": payload.query, "start_urls": payload.start_urls},
        organization_id=_org_id(user),
        project_id=payload.project_id,
        idempotency_key=f"research:{uuid.uuid4().hex[:12]}",
    )
    return {"job_id": job.id, "status": job.status, "created": created}


@router.get("/jobs")
def list_jobs(limit: int = 20, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    query = select(Job).where(Job.type == "research").order_by(Job.created_at.desc()).limit(min(limit, 100))
    org = _org_id(user)
    if org is not None:
        query = query.where(Job.organization_id == org)
    rows = db.execute(query).scalars().all()
    return {
        "count": len(rows),
        "jobs": [
            {"id": j.id, "status": j.status, "stage": j.stage,
             "stats": j.stats, "error": j.error}
            for j in rows
        ],
    }


@router.get("/stores")
def list_stores(limit: int = 50, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    query = select(ExternalStore).order_by(ExternalStore.last_crawled_at.desc().nullslast()).limit(min(limit, 200))
    org = _org_id(user)
    if org is not None:
        query = query.where(ExternalStore.organization_id == org)
    rows = db.execute(query).scalars().all()
    counts = {
        store_id: count
        for store_id, count in db.execute(
            select(ExternalProduct.store_id, func.count(ExternalProduct.id))
            .group_by(ExternalProduct.store_id)
        ).all()
    }
    return {
        "count": len(rows),
        "stores": [
            {"id": s.id, "domain": s.domain, "name": s.name, "niche": s.niche,
             "platform": s.platform, "crawl_status": s.crawl_status,
             "product_count": counts.get(s.id, 0)}
            for s in rows
        ],
    }


@router.get("/opportunities")
def opportunities(
    kind: str | None = None,
    limit: int = 50,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    rows = list_opportunities(db, limit=min(limit, 200), kind=kind, organization_id=_org_id(user))
    return {
        "count": len(rows),
        "opportunities": [
            {"id": o.id, "type": o.type, "title": o.title, "summary": o.summary,
             "score": o.score, "confidence": o.confidence,
             "competition_level": o.competition_level, "status": o.status,
             "source_urls": o.source_urls[:5]}
            for o in rows
        ],
    }


@router.get("/opportunities/{opportunity_id}")
def opportunity_detail(opportunity_id: int, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    row = get_opportunity(db, opportunity_id)
    org = _org_id(user)
    if row is None or (org is not None and row.organization_id != org):
        raise HTTPException(status_code=404, detail="Opportunity not found")
    result: dict[str, Any] = {
        "id": row.id, "type": row.type, "title": row.title, "summary": row.summary,
        "recommended_action": row.recommended_action, "score": row.score,
        "confidence": row.confidence, "evidence": row.evidence,
        "source_urls": row.source_urls, "status": row.status,
    }
    return result


__all__ = ["router"]

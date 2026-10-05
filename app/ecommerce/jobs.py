"""Background work for the product.

Three job types on the shared durable queue:

* ``crawl_shop``        crawl every competitor on a shop, then write a report
* ``crawl_competitor``  crawl one competitor ("crawl now" button)
* ``generate_report``   write a report over a period

Plus ``schedule_due_crawls``, the enqueuer that makes "crawled on a schedule"
true without a human pressing anything.

Every handler opens its own DB session (workers are separate processes) and
drives the durable Job row through its state machine, exactly like the
pre-existing research handler.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.core.database.base import SessionLocal
from app.core.database.domain_models import Job, JobStatus
from app.core.database.ecommerce_models import Competitor, Shop
from app.core.database.models import utcnow
from app.core.jobs.queue import enqueue
from app.core.observability.metrics import inc

logger = logging.getLogger(__name__)


def _load_job(payload: dict):
    """Open a session, fetch the job, and skip work that is already done."""
    job_id = payload.get("job_id")
    db = SessionLocal()
    job = db.get(Job, int(job_id)) if job_id else None
    if job is None or job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
        db.close()
        return None, None
    return db, job


def _fail(db, job: Job, exc: Exception) -> None:
    logger.exception("Job %s (%s) failed", getattr(job, "id", "?"), getattr(job, "type", "?"))
    try:
        db.rollback()
        current = db.get(Job, job.id) if job.id else None
        if current is not None:
            current.status = JobStatus.FAILED.value
            current.error = str(exc)[:2000]
            current.completed_at = utcnow()
            db.commit()
    except Exception:  # noqa: BLE001 — the original failure is what matters
        db.rollback()


def _shop_policy():
    from app.ecommerce.crawl import default_crawl_policy

    return default_crawl_policy()


def crawl_shop(payload: dict) -> None:
    """Crawl every competitor on a shop, then queue a report for the week."""
    from app.ecommerce.crawl import crawl_competitor
    from app.research.crawler import ResponsibleCrawler

    db, job = _load_job(payload)
    if db is None:
        return
    try:
        shop_id = int(payload["shop_id"])
        shop = db.get(Shop, shop_id)
        if shop is None or shop.organization_id is None:
            # A product row without a tenant would break isolation entirely.
            logger.error("Refusing to crawl shop %s with no organization", shop_id)
            return

        competitor_ids = payload.get("competitor_ids") or []
        if competitor_ids:
            competitors = db.execute(
                select(Competitor).where(
                    Competitor.id.in_(competitor_ids),
                    # Scoped to the shop's tenant: a job payload is not trusted.
                    Competitor.organization_id == shop.organization_id,
                )
            ).scalars().all()
        else:
            competitors = db.execute(
                select(Competitor).where(
                    Competitor.organization_id == shop.organization_id,
                    Competitor.shop_id == shop_id,
                    Competitor.is_active.is_(True),
                )
            ).scalars().all()

        job.status = JobStatus.RUNNING.value
        job.stage = f"crawling {len(competitors)} competitor(s)"
        job.started_at = utcnow()
        db.commit()

        # One crawler for the whole shop: the robots cache and the per-host
        # politeness delay are shared, so we never hit a host twice in a row.
        crawler = ResponsibleCrawler(policy=_shop_policy())
        outcomes = []
        try:
            for index, competitor in enumerate(competitors, start=1):
                job.stage = f"crawling {competitor.domain} ({index}/{len(competitors)})"
                job.progress = index / max(1, len(competitors))
                db.commit()
                outcomes.append(
                    crawl_competitor(
                        db, competitor, shop_id=shop_id, crawler=crawler, crawl_id=job.id
                    )
                )
        finally:
            crawler.close()

        totals = {
            "competitors": len(outcomes),
            "products": sum(o.captures_written for o in outcomes),
            "changes": sum(o.changes for o in outcomes),
            "failed": sum(1 for o in outcomes if o.status in ("FAILED", "BLOCKED")),
        }
        _mark_shop_crawled(db, shop)
        job.status = JobStatus.COMPLETED.value
        job.stage = "crawl complete"
        job.progress = 1.0
        job.result = totals
        job.completed_at = utcnow()
        db.commit()
        inc("crawl_jobs_total", outcome="completed")

        # A crawl that found something is exactly when a report is worth writing.
        if totals["changes"] > 0:
            enqueue(
                db,
                type="generate_report",
                payload={"shop_id": shop_id, "days": 7, "kind": "weekly"},
                organization_id=shop.organization_id,
                idempotency_key=f"auto-report:{shop_id}:{utcnow():%Y-%m-%d}",
            )
    except Exception as exc:  # noqa: BLE001
        _fail(db, job, exc)
        inc("crawl_jobs_total", outcome="failed")
        raise
    finally:
        db.close()


def _mark_shop_crawled(db, shop: Shop) -> None:
    """Stamp the shop and push its next scheduled crawl into the future."""
    from datetime import timedelta

    now = utcnow()
    shop.last_crawled_at = now
    shop.next_crawl_at = now + timedelta(hours=shop.crawl_frequency_hours or 168)
    db.commit()


def crawl_one_competitor(payload: dict) -> None:
    """Crawl a single competitor, from the "crawl now" button."""
    from app.ecommerce.crawl import crawl_competitor

    db, job = _load_job(payload)
    if db is None:
        return
    try:
        competitor = db.get(Competitor, int(payload["competitor_id"]))
        if competitor is None or competitor.organization_id is None:
            return
        job.status = JobStatus.RUNNING.value
        job.stage = f"crawling {competitor.domain}"
        job.started_at = utcnow()
        db.commit()

        outcome = crawl_competitor(
            db, competitor, shop_id=payload.get("shop_id"), crawl_id=job.id
        )
        job.status = JobStatus.COMPLETED.value
        job.stage = "complete"
        job.progress = 1.0
        job.result = outcome.to_dict()
        job.completed_at = utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        _fail(db, job, exc)
        raise
    finally:
        db.close()


def generate_report_job(payload: dict) -> None:
    """Write a report over the requested period."""
    from app.ecommerce.reports import generate_report

    db, job = _load_job(payload)
    if db is None:
        return
    try:
        shop = db.get(Shop, int(payload["shop_id"])) if payload.get("shop_id") else None
        org = shop.organization_id if shop else payload.get("organization_id")
        query = select(Competitor).where(Competitor.organization_id == org)
        if shop is not None:
            query = query.where(Competitor.shop_id == shop.id)
        competitors = db.execute(query).scalars().all()

        job.status = JobStatus.RUNNING.value
        job.stage = "writing report"
        job.started_at = utcnow()
        db.commit()

        report = generate_report(
            db,
            organization_id=org,
            shop=shop,
            competitors=list(competitors),
            days=int(payload.get("days") or 7),
            kind=str(payload.get("kind") or "weekly"),
        )
        # D-032: the weekly report is also emailed to the plan owner. This is
        # best-effort by contract -- send_weekly_digest swallows its own errors
        # and claims the report atomically, so a mail failure can never fail
        # the job that produced the report (and a retry cannot double-send).
        if str(payload.get("kind") or "weekly") == "weekly":
            from app.ecommerce.reports import send_weekly_digest

            send_weekly_digest(db, report)
        job.result = {"report_id": report.id, "status": report.status}
        job.status = JobStatus.COMPLETED.value
        job.stage = "report ready"
        job.progress = 1.0
        job.completed_at = utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        _fail(db, job, exc)
        raise
    finally:
        db.close()


def schedule_due_crawls(payload: dict) -> None:
    """Enqueue a crawl for every shop whose next crawl is due.

    This is what makes "crawled on a schedule" true. The scheduler itself just
    enqueues; the per-shop ``crawl:{id}:{date}`` idempotency key means running it
    twice in the same day is harmless.
    """
    from datetime import datetime, timezone

    db = SessionLocal()
    try:
        now = utcnow()
        due = db.execute(
            select(Shop).where(
                Shop.is_active.is_(True),
                Shop.organization_id.isnot(None),
                (Shop.next_crawl_at.is_(None)) | (Shop.next_crawl_at <= now),
            ).limit(100)
        ).scalars().all()

        enqueued = 0
        for shop in due:
            has_competitors = db.execute(
                select(Competitor.id).where(
                    Competitor.organization_id == shop.organization_id,
                    Competitor.shop_id == shop.id,
                    Competitor.is_active.is_(True),
                ).limit(1)
            ).first()
            if has_competitors is None:
                continue
            _, created = enqueue(
                db,
                type="crawl_shop",
                payload={"shop_id": shop.id},
                organization_id=shop.organization_id,
                idempotency_key=f"crawl:{shop.id}:{now:%Y-%m-%d}",
            )
            if created:
                enqueued += 1
                # Move the schedule forward even if enqueue was deduplicated,
                # otherwise we re-check the same shop every tick forever.
                from datetime import timedelta

                shop.next_crawl_at = now + timedelta(hours=shop.crawl_frequency_hours or 168)
                db.commit()
        if enqueued:
            logger.info("Scheduler enqueued %d crawl(s)", enqueued)
            inc("scheduled_crawls_total", value=enqueued)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduler tick failed")
        db.rollback()
    finally:
        db.close()

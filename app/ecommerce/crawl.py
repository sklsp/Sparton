"""Crawl orchestration: fetch a competitor, store captures, detect changes.

The crawl itself is the existing `ResponsibleCrawler` — robots.txt, SSRF guard,
per-host delay, depth and page caps all unchanged (docs/DECISIONS.md D-016).
This module is the product logic *around* it:

    crawl a competitor
      -> for each product found, insert a new CompetitorProduct capture
      -> diff it against the previous capture
      -> write ChangeEvent rows for anything that moved
      -> report how many products appeared and disappeared

Two ordering decisions matter:

1. **Every capture is committed before any diff is computed.** If the process
   dies mid-crawl, the database holds a consistent prefix of a crawl rather
   than a half-applied diff that would generate phantom "price drops".
2. **All captures from one crawl share one timestamp** (the crawl's start),
   not `utcnow()` per row. "The previous capture" is then a clean
   `captured_at < crawl_start` rather than a race against our own writes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database.ecommerce_models import (
    ChangeKind,
    Competitor,
    CompetitorProduct,
    CrawlStatus,
)
from app.core.database.models import utcnow
from app.core.observability.metrics import inc, observe
from app.ecommerce.changes import (
    diff_captures,
    latest_capture_by_name,
    new_product_change,
    normalize_name,
    previous_capture,
    record_changes,
    removed_product_change,
)
from app.research.crawler import CrawlPolicy, ResponsibleCrawler
from app.research.extraction import ExtractedProduct

logger = logging.getLogger(__name__)

#: Fallback page budget when nothing more specific is configured.
DEFAULT_MAX_PAGES = 40


@dataclass(slots=True)
class CrawlOutcome:
    """What one competitor crawl produced."""

    competitor_id: int
    domain: str
    status: str
    pages_fetched: int = 0
    products_found: int = 0
    captures_written: int = 0
    new_products: int = 0
    removed_products: int = 0
    changes: int = 0
    error: str = ""
    duration_ms: int = 0
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "competitor_id": self.competitor_id,
            "domain": self.domain,
            "status": self.status,
            "pages_fetched": self.pages_fetched,
            "products_found": self.products_found,
            "captures_written": self.captures_written,
            "new_products": self.new_products,
            "removed_products": self.removed_products,
            "changes": self.changes,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "skipped": self.skipped,
        }


def default_crawl_policy(max_pages: int | None = None) -> CrawlPolicy:
    """A crawler policy sized for storefronts, not whole websites.

    Depth 1 is deliberate: we want the listing page and the product pages it
    links to, not a site map. The delay is a politeness floor between requests
    to the same host.
    """
    return CrawlPolicy(
        user_agent=settings.crawler_user_agent,
        timeout_seconds=12.0,
        max_retries=2,
        delay_seconds=1.0,
        max_pages=max_pages or settings.crawler_max_pages_per_shop or DEFAULT_MAX_PAGES,
        max_depth=1,
        max_body_bytes=2_000_000,
        allow_private_addresses=settings.crawler_allow_private_addresses,
    )


def capture_fields(product: ExtractedProduct, fallback_url: str) -> dict[str, Any]:
    """Map an extracted page product onto our capture columns."""
    availability = (product.availability or "unknown").lower()
    in_stock = availability not in {"outofstock", "out_of_stock", "soldout", "discontinued"}
    sku = product.attributes.get("sku") or product.attributes.get("mpn") or ""
    return {
        "source_url": product.url or fallback_url,
        "external_id": str(sku)[:160],
        "name": (product.name or "Untitled product")[:400],
        "normalized_name": normalize_name(product.name),
        "brand": (product.brand or "")[:200],
        "category": (product.category or "")[:160],
        "price": product.price,
        "currency": (product.currency or "").upper()[:8] or "EUR",
        "availability": availability,
        "in_stock": in_stock,
        "image_url": product.image_url or "",
        "description": (product.description or "")[:2000],
        "extraction_method": product.method,
        "confidence": product.confidence,
    }


def dedupe_products(products: list[ExtractedProduct]) -> list[ExtractedProduct]:
    """One row per product URL.

    A listing page and the product page it links to both yield the same item,
    and the same product often appears twice in one collection. Prefer the copy
    that actually has a price.
    """
    by_url: dict[str, ExtractedProduct] = {}
    unkeyed: list[ExtractedProduct] = []
    for product in products:
        if not product.url:
            unkeyed.append(product)
            continue
        existing = by_url.get(product.url)
        if existing is None or (product.price and not existing.price):
            by_url[product.url] = product
    return list(by_url.values()) + unkeyed


def _last_capture(
    db: Session, competitor_id: int, source_url: str, normalized_name: str, before: datetime
) -> CompetitorProduct | None:
    """The most recent capture of this product before `before`.

    URL is the authoritative key. The normalized name is the fallback for sites
    that rewrite product URLs between crawls (common on Shopify, where a
    handle change is invisible to the customer).
    """
    found = previous_capture(db, competitor_id, source_url, before)
    if found is not None:
        return found
    return latest_capture_by_name(db, competitor_id, normalized_name, before)


def _previous_crawl_urls(db: Session, competitor_id: int, before: datetime) -> set[str]:
    """Every product URL this competitor had before the current crawl started.

    Two queries rather than one window function, because this has to run on
    both SQLite and PostgreSQL and the number of rows is small (a few hundred
    per competitor).
    """
    last_time = db.execute(
        select(func.max(CompetitorProduct.captured_at)).where(
            CompetitorProduct.competitor_id == competitor_id,
            CompetitorProduct.captured_at < before,
        )
    ).scalar()
    if last_time is None:
        return set()
    return {
        url
        for (url,) in db.execute(
            select(CompetitorProduct.source_url).where(
                CompetitorProduct.competitor_id == competitor_id,
                CompetitorProduct.captured_at == last_time,
            )
        ).all()
        if url
    }


def crawl_competitor(
    db: Session,
    competitor: Competitor,
    *,
    shop_id: int | None = None,
    max_pages: int | None = None,
    crawler: ResponsibleCrawler | None = None,
    crawl_id: int | None = None,
) -> CrawlOutcome:
    """Crawl one competitor, write captures, and record any changes.

    Never raises for an ordinary failure — a site that is down, a robots denial,
    a timeout. Those land on the competitor's ``last_status`` so the customer
    can see *why* nothing updated, and the caller moves on to the next
    competitor. One dead competitor must not fail a customer's week.
    """
    started = time.monotonic()
    outcome = CrawlOutcome(
        competitor_id=competitor.id, domain=competitor.domain, status=CrawlStatus.RUNNING
    )
    owns_crawler = crawler is None
    crawler = crawler or ResponsibleCrawler(policy=default_crawl_policy(max_pages))
    # One timestamp for the whole crawl, so "the previous capture" is a clean
    # inequality rather than a race against our own inserts.
    captured_at = utcnow()

    try:
        if not competitor.is_active:
            outcome.status, outcome.skipped = CrawlStatus.COMPLETED, True
            return _finish(db, competitor, outcome, started)

        start_url = competitor.url or f"https://{competitor.domain}"
        results = crawler.crawl([start_url])
        outcome.pages_fetched = len(results)
        if not results or all(r.error for r in results):
            blocked = any(r.error and "robots" in r.error.lower() for r in results)
            outcome.status = CrawlStatus.BLOCKED if blocked else CrawlStatus.FAILED
            outcome.error = next((r.error for r in results if r.error), "no pages fetched")
            return _finish(db, competitor, outcome, started)

        products = dedupe_products([p for r in results for p in r.products])
        outcome.products_found = len(products)
        current_urls: set[str] = set()

        # --- 1. write every capture, commit ------------------------------
        rows: list[CompetitorProduct] = []
        for product in products:
            fields = capture_fields(product, start_url)
            row = CompetitorProduct(
                organization_id=competitor.organization_id,
                competitor_id=competitor.id,
                crawl_id=crawl_id,
                captured_at=captured_at,
                **fields,
            )
            db.add(row)
            rows.append(row)
        db.commit()
        for row in rows:
            db.refresh(row)
        outcome.captures_written = len(rows)

        # --- 2. now diff, against a database that already has the captures --
        detected = []
        for row in rows:
            before = _last_capture(
                db, competitor.id, row.source_url, row.normalized_name, captured_at
            )
            if before is None:
                detected.append(new_product_change(row, competitor=competitor))
            else:
                detected.extend(diff_captures(before, row, competitor=competitor))
            current_urls.add(row.source_url)

        # Removals need the *previous crawl's whole URL set*, not just the ones
        # that still exist — a product that is gone is, by definition, not in
        # `rows`. Comparing against only the matched set would find nothing.
        previous_urls = _previous_crawl_urls(db, competitor.id, captured_at)
        for url in sorted(previous_urls - current_urls)[:200]:
            last = _last_capture(db, competitor.id, url, "", captured_at)
            if last is not None:
                detected.append(removed_product_change(last, competitor=competitor))

        created = record_changes(
            db,
            organization_id=competitor.organization_id,
            shop_id=shop_id,
            competitor_id=competitor.id,
            changes=detected,
            detected_at=captured_at,
        )
        outcome.changes = len(created)
        outcome.new_products = sum(1 for c in created if c.kind == ChangeKind.NEW_PRODUCT)
        outcome.removed_products = sum(
            1 for c in created if c.kind == ChangeKind.REMOVED_PRODUCT
        )

        # First ever crawl: every product is "new", which is not news. Say so
        # once, as a baseline, instead of flooding the customer's inbox.
        if competitor.last_crawled_at is None and created:
            _demote_to_baseline(db, created, competitor, len(rows))

        outcome.status = CrawlStatus.COMPLETED
        inc("crawl_competitors_total", outcome="completed")
        return _finish(db, competitor, outcome, started)

    except Exception as exc:  # noqa: BLE001 — one competitor must not fail the week
        logger.exception("Crawl failed for %s", competitor.domain)
        outcome.status = CrawlStatus.FAILED
        outcome.error = str(exc)[:500]
        inc("crawl_competitors_total", outcome="failed")
        return _finish(db, competitor, outcome, started)
    finally:
        if owns_crawler:
            crawler.close()


def _demote_to_baseline(db: Session, created, competitor: Competitor, total: int) -> None:
    """Rewrite a first crawl's "new product" rows into one baseline statement."""
    label = competitor.name or competitor.domain
    for row in created:
        row.kind = ChangeKind.NEW_PRODUCT
        row.severity = "low"
        row.title = f"Baseline set: {label} has {total} products"
        row.summary = (
            "This was your first crawl of this competitor, so everything is new. "
            "Real changes will appear here from the next crawl onwards."
        )
    db.commit()


def _finish(
    db: Session, competitor: Competitor, outcome: CrawlOutcome, started: float
) -> CrawlOutcome:
    """Stamp the outcome onto the competitor row. Must not itself raise."""
    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    observe("crawl_competitor_duration_seconds", outcome.duration_ms / 1000)
    try:
        competitor.last_status = outcome.status
        competitor.last_crawled_at = utcnow()
        competitor.product_count = max(competitor.product_count, outcome.captures_written)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("Could not record crawl outcome for %s", competitor.domain)
    return outcome

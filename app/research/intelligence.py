"""Odysseus — research & market intelligence pipeline.

Adapted from Ares `services/intelligence.py`: discovery → crawl → extract →
persist → explainable opportunity scoring. Rewired to SPARTON models and the
shared durable Job system (a ``research`` job row in ``jobs`` carries the
query; stats/progress live on the Job).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.core.database.creation_models import GeneratedImage  # noqa: F401 (consistency)
from app.core.database.domain_models import (
    ExternalProduct,
    ExternalStore,
    Job,
    Opportunity,
    OpportunityEvidence,
    Product,
    ProductSnapshot,
)
from app.research.crawler import CrawlPolicy, ResponsibleCrawler
from app.research.discovery import DuckDuckGoProvider, SearchProvider


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _store_name(result, domain: str) -> str:
    return result.metadata.get("title", "").strip() or domain


def _upsert_product(db: Session, store: ExternalStore, product, captured_at: datetime) -> ExternalProduct:
    existing = db.execute(
        select(ExternalProduct).where(ExternalProduct.source_url == product.url)
    ).scalars().first()
    if existing is None:
        existing = ExternalProduct(
            store_id=store.id,
            source_url=product.url or store.domain,
            name=product.name or "Untitled product",
            normalized_name=normalize_name(product.name),
            brand=product.brand,
            category=product.category,
            price=product.price,
            currency=product.currency,
            availability=product.availability,
            description=product.description,
            image_url=product.image_url,
            attributes=product.attributes,
            confidence=product.confidence,
            first_seen_at=captured_at,
            last_seen_at=captured_at,
        )
        db.add(existing)
        db.flush()
    else:
        existing.last_seen_at = captured_at
        existing.name = product.name or existing.name
        existing.normalized_name = normalize_name(existing.name)
        existing.price = product.price
        existing.currency = product.currency or existing.currency
        existing.availability = product.availability
        existing.description = product.description
        existing.image_url = product.image_url
        existing.attributes = product.attributes
        existing.confidence = max(existing.confidence, product.confidence)
    db.add(ProductSnapshot(
        external_product_id=existing.id,
        price=product.price,
        availability=product.availability,
        review_count=product.review_count,
        raw={"method": product.method},
        captured_at=captured_at,
    ))
    return existing


def _upsert_store(db: Session, domain: str, niche: str, name: str,
                  organization_id: int | None = None) -> ExternalStore:
    query = select(ExternalStore).where(ExternalStore.domain == domain)
    if organization_id is not None:
        query = query.where(ExternalStore.organization_id == organization_id)
    else:
        query = query.where(ExternalStore.organization_id.is_(None))
    store = db.execute(query).scalars().first()
    if store is None:
        store = ExternalStore(domain=domain, name=name, niche=niche,
                              organization_id=organization_id)
        db.add(store)
        db.flush()
    elif name and store.name == store.domain:
        store.name = name
    return store


def _opportunity(db: Session, job: Job, kind: str, title: str, summary: str, action: str,
                 score: float, confidence: float, sources: list[str],
                 evidence: dict[str, Any], competition: str,
                 signals: dict[str, Any]) -> Opportunity:
    item = Opportunity(
        job_id=job.id,
        organization_id=job.organization_id,
        type=kind,
        title=title,
        summary=summary,
        recommended_action=action,
        source_urls=list(dict.fromkeys(sources)),
        evidence=evidence,
        score=round(max(0, min(100, score)), 1),
        confidence=round(max(0, min(1, confidence)), 2),
        competition_level=competition,
        demand_signals=signals,
    )
    db.add(item)
    db.flush()
    for source in item.source_urls:
        db.add(OpportunityEvidence(
            opportunity_id=item.id,
            source_url=source,
            source_domain=urlparse(source).netloc,
            claim=summary,
            extraction_method="crawler+explainable-score",
            observed_value=evidence,
            confidence=item.confidence,
        ))
    return item


def run_investigation(db: Session, job: Job, *, start_urls: list[str] | None = None,
                      search: SearchProvider | None = None,
                      crawler: ResponsibleCrawler | None = None) -> dict[str, int]:
    """Execute one research job end-to-end. The Job row is the state record."""
    search = search or DuckDuckGoProvider()
    crawler = crawler or ResponsibleCrawler(CrawlPolicy(
        max_pages=20,
        max_depth=1,
        delay_seconds=0.5,
        allow_private_addresses=app_settings.crawler_allow_private_addresses,
    ))
    query = str(job.payload.get("query", ""))

    try:
        job.status = "RUNNING"
        job.stage = "discovering sources"
        job.started_at = utcnow()
        db.commit()

        urls = list(dict.fromkeys(start_urls or search.search(query, limit=8)))
        if not urls:
            raise RuntimeError("No public sources were discovered for this query")

        job.stats = {
            "domains_discovered": len({urlparse(url).netloc for url in urls}),
            "pages_discovered": len(urls),
            "pages_crawled": 0,
            "products_discovered": 0,
            "opportunities_found": 0,
        }
        db.commit()

        results = crawler.crawl(urls)
        products: list[ExternalProduct] = []
        for result in results:
            job.stats = {**job.stats, "pages_crawled": job.stats.get("pages_crawled", 0) + 1}
            if result.error or not result.products:
                continue
            domain = urlparse(result.url).netloc.lower()
            store = _upsert_store(db, domain, query, _store_name(result, domain),
                                  organization_id=job.organization_id)
            store.platform = "shopify" if "shopify" in str(result.metadata).lower() else store.platform
            store.last_crawled_at = utcnow()
            store.crawl_status = "ok"
            for extracted in result.products:
                extracted.url = extracted.url or result.url
                products.append(_upsert_product(db, store, extracted, utcnow()))
            db.commit()

        job.stage = "scoring opportunities"
        db.commit()

        grouped: dict[str, list[ExternalProduct]] = defaultdict(list)
        for product in products:
            if product.normalized_name:
                grouped[product.normalized_name].append(product)

        local_names = {normalize_name(row[0]) for row in db.execute(select(Product.title)).all()}
        found = 0
        for name, matches in grouped.items():
            domains = sorted({p.store.domain for p in matches if p.store})
            if len(domains) < 2 and name in local_names:
                continue
            repetition = min(len(domains) / 5, 1)
            gap = 1 if name not in local_names else 0
            price_values = [p.price for p in matches if p.price is not None]
            median_price = sorted(price_values)[len(price_values) // 2] if price_values else None
            score = 35 + repetition * 35 + gap * 20 + (10 if median_price else 0)
            kind = "ASSORTMENT_GAP" if gap else "PRODUCT_OPPORTUNITY"
            title = f"{matches[0].name} market signal"
            summary = (
                f"Observed across {len(domains)} public store(s). "
                + ("Your catalog has no normalized match." if gap
                   else "A comparable item is already in your catalog.")
            )
            evidence = {
                "stores_observed": len(domains),
                "products_observed": len(matches),
                "median_price": median_price,
                "observed": True,
                "inferred": "repeated public listings indicate market presence",
            }
            _opportunity(db, job, kind, title, summary,
                         "Review the evidence and compare this cluster with the catalog "
                         "before considering an approved addition.",
                         score, min(0.95, 0.45 + repetition * 0.4),
                         [p.source_url for p in matches], evidence,
                         "moderate" if len(domains) < 5 else "high",
                         {"repetition": len(matches), "domains": domains})
            found += 1

        job.stats = {**job.stats, "products_discovered": len(products), "opportunities_found": found}
        job.status = "COMPLETED"
        job.stage = "complete"
        job.completed_at = utcnow()
        job.result = {"opportunities_found": found}
        db.commit()
        return {"pages": len(results), "products": len(products), "opportunities": found}
    except Exception as exc:
        job.status = "FAILED"
        job.stage = "failed"
        job.error = str(exc)
        job.completed_at = utcnow()
        db.commit()
        raise
    finally:
        if hasattr(search, "close"):
            search.close()
        crawler.close()


def list_opportunities(db: Session, limit: int = 50, kind: str | None = None,
                       organization_id: int | None = None) -> list[Opportunity]:
    stmt = select(Opportunity).order_by(Opportunity.score.desc(), Opportunity.discovered_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(Opportunity.type == kind)
    if organization_id is not None:
        stmt = stmt.where(Opportunity.organization_id == organization_id)
    return list(db.execute(stmt).scalars())


def get_opportunity(db: Session, opportunity_id: int) -> Opportunity | None:
    return db.execute(select(Opportunity).where(Opportunity.id == opportunity_id)).scalar_one_or_none()

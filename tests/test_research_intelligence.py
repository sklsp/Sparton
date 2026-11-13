"""Tests for app.research.intelligence: investigation pipeline and opportunity queries."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.database.domain_models import (
    ExternalProduct,
    ExternalStore,
    Job,
    Opportunity,
    OpportunityEvidence,
    Product,
    ProductSnapshot,
)
from app.research.crawler import CrawlResult
from app.research.extraction import ExtractedProduct
from app.research.intelligence import (
    get_opportunity,
    list_opportunities,
    normalize_name,
    run_investigation,
)


class FakeSearch:
    """Stands in for DuckDuckGoProvider; records calls and close()."""

    def __init__(self, urls):
        self.urls = list(urls)
        self.calls = []
        self.closed = False

    def search(self, query, limit=10):
        self.calls.append((query, limit))
        return list(self.urls)

    def close(self):
        self.closed = True


class FakeCrawler:
    """Stands in for ResponsibleCrawler; returns canned CrawlResults."""

    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.crawled_urls = None
        self.closed = False

    def crawl(self, urls):
        self.crawled_urls = list(urls)
        if self.error is not None:
            raise self.error
        return self.results

    def close(self):
        self.closed = True


def _job(db_session, query="ceramic mugs", organization_id=None):
    job = Job(organization_id=organization_id, type="research", payload={"query": query})
    db_session.add(job)
    db_session.commit()
    return job


def _product(name, url, price, currency="USD", confidence=0.9, availability="in stock"):
    return ExtractedProduct(
        name=name, url=url, price=price, currency=currency,
        availability=availability, method="json-ld", confidence=confidence,
    )


def _result(url, products=None, title="", error=None):
    metadata = {"title": title} if title else {}
    return CrawlResult(
        url=url, status_code=None if error else 200,
        products=list(products or []), links=[], metadata=metadata, error=error,
    )


def test_run_investigation_writes_stores_products_and_opportunity(db_session):
    job = _job(db_session, organization_id=7)
    search = FakeSearch(["https://a.example/collections/all", "https://b.example/shop"])
    crawler = FakeCrawler([
        _result("https://a.example/products/mug-page",
                products=[_product("Ceramic Mug", "https://a.example/products/mug", 12.5)],
                title="Store A"),
        # No page title: the store name must fall back to the domain.
        _result("https://b.example/products/mug-page",
                products=[_product("Ceramic Mug", "https://b.example/products/mug", 14.0, confidence=0.8)]),
    ])

    summary = run_investigation(db_session, job, search=search, crawler=crawler)

    assert summary == {"pages": 2, "products": 2, "opportunities": 1}

    stores = {s.domain: s for s in db_session.execute(select(ExternalStore)).scalars()}
    assert set(stores) == {"a.example", "b.example"}
    assert stores["a.example"].name == "Store A"
    assert stores["b.example"].name == "b.example"
    for store in stores.values():
        assert store.organization_id == 7
        assert store.niche == "ceramic mugs"
        assert store.platform == "unknown"  # no shopify marker in either page's metadata
        assert store.crawl_status == "ok"
        assert store.last_crawled_at is not None

    products = {p.source_url: p for p in db_session.execute(select(ExternalProduct)).scalars()}
    assert set(products) == {"https://a.example/products/mug", "https://b.example/products/mug"}
    a_product = products["https://a.example/products/mug"]
    assert a_product.name == "Ceramic Mug"
    assert a_product.normalized_name == "ceramic mug"
    assert a_product.price == 12.5
    assert a_product.currency == "USD"
    assert a_product.availability == "in stock"
    assert a_product.confidence == 0.9
    assert a_product.store_id == stores["a.example"].id
    assert a_product.first_seen_at is not None
    assert a_product.first_seen_at == a_product.last_seen_at

    snapshots = db_session.execute(select(ProductSnapshot)).scalars().all()
    assert len(snapshots) == 2
    for snapshot in snapshots:
        assert snapshot.raw == {"method": "json-ld"}
        assert snapshot.captured_at is not None
    prices = sorted(s.price for s in snapshots)
    assert prices == [12.5, 14.0]

    opportunities = db_session.execute(select(Opportunity)).scalars().all()
    assert len(opportunities) == 1
    opportunity = opportunities[0]
    # Two domains: repetition = min(2/5, 1); not in catalog -> gap; median price present.
    assert opportunity.type == "ASSORTMENT_GAP"
    assert opportunity.title == "Ceramic Mug market signal"
    assert opportunity.score == 79.0
    # Rounded to two decimals by _opportunity: min(0.95, 0.45 + (2/5) * 0.4).
    assert opportunity.confidence == 0.61
    assert opportunity.competition_level == "moderate"
    assert opportunity.organization_id == 7
    assert opportunity.job_id == job.id
    assert opportunity.source_urls == [
        "https://a.example/products/mug", "https://b.example/products/mug"]
    assert opportunity.evidence["stores_observed"] == 2
    assert opportunity.evidence["products_observed"] == 2
    assert opportunity.evidence["median_price"] == 14.0
    assert opportunity.evidence["observed"] is True
    assert opportunity.demand_signals == {"repetition": 2, "domains": ["a.example", "b.example"]}

    # One evidence row per source URL, carrying the explainable-score payload.
    evidence_rows = {e.source_domain: e for e in db_session.execute(select(OpportunityEvidence)).scalars()}
    assert set(evidence_rows) == {"a.example", "b.example"}
    row = evidence_rows["a.example"]
    assert row.opportunity_id == opportunity.id
    assert row.source_url == "https://a.example/products/mug"
    assert row.claim == opportunity.summary
    assert row.extraction_method == "crawler+explainable-score"
    assert row.observed_value["stores_observed"] == 2
    assert row.confidence == opportunity.confidence

    assert job.status == "COMPLETED"
    assert job.stage == "complete"
    assert job.started_at is not None
    assert job.completed_at is not None
    assert job.result == {"opportunities_found": 1}
    assert job.stats == {
        "domains_discovered": 2,
        "pages_discovered": 2,
        "pages_crawled": 2,
        "products_discovered": 2,
        "opportunities_found": 1,
    }

    assert search.calls == [("ceramic mugs", 8)]
    assert crawler.crawled_urls == ["https://a.example/collections/all", "https://b.example/shop"]
    # The finally block closes both collaborators, even on success.
    assert search.closed is True
    assert crawler.closed is True


def test_run_investigation_second_run_updates_instead_of_duplicating(db_session):
    job1 = _job(db_session)
    run_investigation(
        db_session, job1,
        search=FakeSearch(["https://a.example/collections/all"]),
        crawler=FakeCrawler([_result("https://a.example/products/mug-page",
                                     products=[_product("Ceramic Mug", "https://a.example/products/mug", 12.5)])]),
    )

    job2 = _job(db_session)
    deluxe = _product("Ceramic Mug Deluxe", "https://a.example/products/mug", 13.0, confidence=0.7)
    summary = run_investigation(
        db_session, job2,
        search=FakeSearch(["https://a.example/collections/all"]),
        crawler=FakeCrawler([_result("https://a.example/products/mug-page", products=[deluxe])]),
    )

    assert summary == {"pages": 1, "products": 1, "opportunities": 1}

    stores = db_session.execute(select(ExternalStore)).scalars().all()
    assert len(stores) == 1
    assert stores[0].last_crawled_at is not None

    products = db_session.execute(select(ExternalProduct)).scalars().all()
    # Same source_url: the row was updated, not duplicated.
    assert len(products) == 1
    product = products[0]
    assert product.name == "Ceramic Mug Deluxe"
    assert product.normalized_name == "ceramic mug deluxe"
    assert product.price == 13.0
    # Confidence only ever increases across runs: max(0.9, 0.7).
    assert product.confidence == 0.9
    assert product.first_seen_at <= product.last_seen_at

    snapshots = db_session.execute(select(ProductSnapshot)).scalars().all()
    # One snapshot per run; the newest carries the updated price.
    assert len(snapshots) == 2
    latest = max(snapshots, key=lambda s: s.captured_at)
    assert latest.price == 13.0

    # Each run's product name forms its own single-domain gap cluster (no catalog match).
    opportunities = db_session.execute(select(Opportunity)).scalars().all()
    assert len(opportunities) == 2


def test_run_investigation_crawl_failure_fails_job_and_still_closes(db_session):
    job = _job(db_session)
    search = FakeSearch(["https://a.example/collections/all"])
    crawler = FakeCrawler(error=RuntimeError("network down"))

    with pytest.raises(RuntimeError, match="network down"):
        run_investigation(db_session, job, search=search, crawler=crawler)

    assert job.status == "FAILED"
    assert job.stage == "failed"
    assert job.error == "network down"
    assert job.completed_at is not None
    # The finally block closes both collaborators even when the crawl raises.
    assert search.closed is True
    assert crawler.closed is True


def test_run_investigation_no_discovered_sources_fails_job(db_session):
    job = _job(db_session)
    crawler = FakeCrawler()

    with pytest.raises(RuntimeError, match="No public sources"):
        run_investigation(db_session, job, search=FakeSearch([]), crawler=crawler)

    assert job.status == "FAILED"
    assert job.stage == "failed"
    assert "No public sources were discovered" in (job.error or "")
    # Nothing was found, so the crawler is never invoked.
    assert crawler.crawled_urls is None


def test_run_investigation_skips_failed_and_empty_pages(db_session):
    job = _job(db_session)
    crawler = FakeCrawler([
        _result("https://blocked.example/", error="Blocked by robots.txt"),
        _result("https://empty.example/"),
        _result("https://c.example/products/mug-page",
                products=[_product("Ceramic Mug", "https://c.example/products/mug", 12.5)]),
    ])

    summary = run_investigation(db_session, job, search=FakeSearch(["x"]), crawler=crawler)

    assert summary == {"pages": 3, "products": 1, "opportunities": 1}
    stores = db_session.execute(select(ExternalStore)).scalars().all()
    # Only the page that actually yielded products creates a store.
    assert [s.domain for s in stores] == ["c.example"]
    assert job.stats["pages_crawled"] == 3


def test_run_investigation_skips_single_store_match_already_in_catalog(db_session):
    db_session.add(Product(organization_id=None, sku="SKU-1", title="Ceramic Mug",
                           description="house mug", price=9.0, category="test"))
    db_session.commit()

    job = _job(db_session)
    summary = run_investigation(
        db_session, job,
        search=FakeSearch(["https://a.example/collections/all"]),
        crawler=FakeCrawler([_result("https://a.example/products/mug-page",
                                     products=[_product("Ceramic Mug", "https://a.example/products/mug", 12.5)])]),
    )

    # One store only and the catalog already carries this name: no opportunity.
    assert summary == {"pages": 1, "products": 1, "opportunities": 0}
    assert db_session.execute(select(Opportunity)).scalars().all() == []
    assert job.status == "COMPLETED"
    assert job.result == {"opportunities_found": 0}


def test_run_investigation_flags_catalog_match_as_product_opportunity(db_session):
    db_session.add(Product(organization_id=None, sku="SKU-1", title="Ceramic Mug",
                           description="house mug", price=9.0, category="test"))
    db_session.commit()

    job = _job(db_session)
    run_investigation(
        db_session, job,
        search=FakeSearch(["https://a.example/collections/all"]),
        crawler=FakeCrawler([
            _result("https://a.example/products/mug-page",
                    products=[_product("Ceramic Mug", "https://a.example/products/mug", 12.5)]),
            _result("https://b.example/products/mug-page",
                    products=[_product("Ceramic Mug", "https://b.example/products/mug", 14.0)]),
        ]),
    )

    opportunities = db_session.execute(select(Opportunity)).scalars().all()
    assert len(opportunities) == 1
    opportunity = opportunities[0]
    # Two domains but the catalog already has it: PRODUCT_OPPORTUNITY, no gap bonus.
    assert opportunity.type == "PRODUCT_OPPORTUNITY"
    assert opportunity.score == 59.0
    assert "A comparable item is already in your catalog." in opportunity.summary


def test_list_opportunities_orders_filters_and_limits(db_session):
    def _opp(score, type_, organization_id=None):
        opp = Opportunity(
            organization_id=organization_id, type=type_, title=f"t{score}", summary="s",
            recommended_action="", source_urls=[], evidence={}, score=score,
            confidence=0.5, competition_level="low",
        )
        db_session.add(opp)
        return opp

    _opp(50, "ASSORTMENT_GAP")
    _opp(80, "PRODUCT_OPPORTUNITY")
    _opp(70, "ASSORTMENT_GAP")
    org1 = _opp(60, "ASSORTMENT_GAP", organization_id=1)
    _opp(90, "ASSORTMENT_GAP", organization_id=2)
    db_session.commit()

    all_rows = list_opportunities(db_session)
    assert [o.score for o in all_rows] == [90.0, 80.0, 70.0, 60.0, 50.0]

    by_kind = list_opportunities(db_session, kind="PRODUCT_OPPORTUNITY")
    assert [o.score for o in by_kind] == [80.0]

    limited = list_opportunities(db_session, limit=1)
    assert [o.score for o in limited] == [90.0]

    only_org1 = list_opportunities(db_session, organization_id=1)
    assert [o.id for o in only_org1] == [org1.id]


def test_get_opportunity_returns_row_or_none(db_session):
    opp = Opportunity(
        type="ASSORTMENT_GAP", title="t", summary="s", recommended_action="",
        source_urls=[], evidence={}, score=42.0, confidence=0.5, competition_level="low",
    )
    db_session.add(opp)
    db_session.commit()

    assert get_opportunity(db_session, opp.id).id == opp.id
    assert get_opportunity(db_session, 424242) is None


def test_normalize_name_collapses_punctuation_and_case():
    assert normalize_name("Ceramic MUGS & More!") == "ceramic mugs more"
    assert normalize_name("--weird__name--") == "weird name"
    assert normalize_name("ABC 123") == "abc 123"
    assert normalize_name("") == ""

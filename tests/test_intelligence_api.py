"""Intelligence API (app.api.intelligence): research jobs, stores, opportunities."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.database.domain_models import (
    ExternalProduct,
    ExternalStore,
    Job,
    Opportunity,
    utcnow,
)
from app.core.database.identity import User

# Same admin email that intelligence_headers registers below (conftest's TEST_ADMIN_EMAIL).
ADMIN_EMAIL = "admin@example.com"


@pytest.fixture()
def intelligence_client(db_session, llm, monkeypatch):
    """A client with the research domain enabled so /intelligence/* routes exist.

    The default deployment does not enable research: app.main gates this router on
    Domain.RESEARCH. The experimental domains need requirements-experimental.txt,
    which the product image and CI do not install, so this enables only product
    domains plus research and needs no experimental imports.
    """
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "enabled_domains", "intelligence,commerce,agent,research")
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture()
def intelligence_headers(intelligence_client):
    """Auth headers for a fresh org admin against the research-enabled client."""
    response = intelligence_client.post(
        "/auth/register",
        json={"email": ADMIN_EMAIL, "password": "correct-horse-battery",
              "organization_name": "Test Org"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _org_id(db_session) -> int:
    user = db_session.execute(select(User).where(User.email == ADMIN_EMAIL)).scalars().first()
    assert user is not None and user.organization_id is not None
    return user.organization_id


# --- POST /intelligence/jobs -------------------------------------------------


def test_start_research_enqueues_a_scoped_job(intelligence_client, intelligence_headers, db_session):
    response = intelligence_client.post(
        "/intelligence/jobs",
        headers=intelligence_headers,
        json={"query": "ceramic mugs market", "start_urls": ["https://a.example/collections/all"]},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["created"] is True

    job = db_session.get(Job, body["job_id"])
    assert job.type == "research"
    assert job.payload == {
        "query": "ceramic mugs market",
        "start_urls": ["https://a.example/collections/all"],
    }
    assert job.organization_id == _org_id(db_session)
    # A fresh idempotency key per request: duplicates are never deduped here.
    assert job.idempotency_key is not None and job.idempotency_key.startswith("research:")


def test_start_research_validates_the_query(intelligence_client, intelligence_headers):
    for payload in ({}, {"query": "ab"}):
        response = intelligence_client.post("/intelligence/jobs", headers=intelligence_headers, json=payload)
        assert response.status_code == 422, (payload, response.text)


# --- GET /intelligence/jobs --------------------------------------------------


def test_list_jobs_returns_only_research_jobs_newest_first(intelligence_client, intelligence_headers, db_session):
    org = _org_id(db_session)
    now = utcnow()

    newest = Job(type="research", payload={"query": "new"}, organization_id=org, created_at=now + timedelta(hours=2))
    older = Job(type="research", payload={"query": "old"}, organization_id=org, created_at=now)
    other_type = Job(type="crawl_shop", payload={}, organization_id=org, created_at=now + timedelta(hours=3))
    foreign = Job(
        type="research", payload={"query": "theirs"}, organization_id=999,
        created_at=now + timedelta(hours=4),
    )
    db_session.add_all([newest, older, other_type, foreign])
    db_session.commit()

    response = intelligence_client.get("/intelligence/jobs", headers=intelligence_headers)
    assert response.status_code == 200
    body = response.json()
    # Only research jobs of the caller's organization, newest first.
    assert [j["id"] for j in body["jobs"]] == [newest.id, older.id]
    assert body["count"] == 2
    entry = body["jobs"][0]
    assert set(entry) == {"id", "status", "stage", "stats", "error"}

    limited = intelligence_client.get("/intelligence/jobs?limit=1", headers=intelligence_headers).json()
    assert [j["id"] for j in limited["jobs"]] == [newest.id]


# --- GET /intelligence/stores -------------------------------------------------


def test_list_stores_orders_and_counts_products(intelligence_client, intelligence_headers, db_session):
    org = _org_id(db_session)
    now = utcnow()

    crawled_recently = ExternalStore(
        domain="recent.example", name="Recent", organization_id=org,
        last_crawled_at=now + timedelta(hours=2), crawl_status="ok",
    )
    crawled_earlier = ExternalStore(
        domain="earlier.example", name="Earlier", organization_id=org,
        last_crawled_at=now + timedelta(hours=1), crawl_status="ok",
    )
    never_crawled = ExternalStore(domain="never.example", name="Never", organization_id=org)
    foreign_store = ExternalStore(
        domain="foreign.example", name="Foreign", organization_id=999,
        last_crawled_at=now + timedelta(hours=5),
    )
    db_session.add_all([crawled_recently, crawled_earlier, never_crawled, foreign_store])
    db_session.flush()

    for i in range(2):
        db_session.add(ExternalProduct(
            store_id=crawled_recently.id, source_url=f"https://recent.example/p{i}",
            name=f"Recent product {i}", normalized_name=f"recent product {i}",
        ))
    db_session.add(ExternalProduct(
        store_id=never_crawled.id, source_url="https://never.example/p0",
        name="Never product 0", normalized_name="never product 0",
    ))
    db_session.commit()

    response = intelligence_client.get("/intelligence/stores", headers=intelligence_headers)
    assert response.status_code == 200
    body = response.json()
    # Newest crawl first, never-crawled last (nullslast); foreign org excluded.
    assert [s["domain"] for s in body["stores"]] == ["recent.example", "earlier.example", "never.example"]
    assert body["count"] == 3

    by_domain = {s["domain"]: s for s in body["stores"]}
    assert by_domain["recent.example"]["product_count"] == 2
    assert by_domain["earlier.example"]["product_count"] == 0
    assert by_domain["never.example"]["product_count"] == 1
    entry = by_domain["recent.example"]
    assert set(entry) == {"id", "domain", "name", "niche", "platform", "crawl_status", "product_count"}


# --- GET /intelligence/opportunities ------------------------------------------


def _opportunity(org: int, *, type_: str, score: float, urls: list[str] | None = None) -> Opportunity:
    return Opportunity(
        organization_id=org,
        type=type_,
        title=f"{type_} {score}",
        summary="summary",
        recommended_action="act on it",
        evidence={"claims": ["claim"]},
        source_urls=urls or [f"https://s.example/{i}" for i in range(3)],
        score=score,
        confidence=0.8,
    )


def test_opportunities_filters_by_kind_limit_and_org(intelligence_client, intelligence_headers, db_session):
    org = _org_id(db_session)

    gap = _opportunity(org, type_="MARKET_GAP", score=80.0, urls=[f"https://s.example/{i}" for i in range(7)])
    product_opp = _opportunity(org, type_="PRODUCT_OPPORTUNITY", score=60.0)
    foreign = _opportunity(999, type_="MARKET_GAP", score=99.0)
    db_session.add_all([gap, product_opp, foreign])
    db_session.commit()

    response = intelligence_client.get("/intelligence/opportunities", headers=intelligence_headers)
    assert response.status_code == 200
    body = response.json()
    # Only the caller's organization, highest score first.
    assert [o["id"] for o in body["opportunities"]] == [gap.id, product_opp.id]
    assert body["count"] == 2

    top = body["opportunities"][0]
    assert set(top) == {
        "id", "type", "title", "summary", "score", "confidence",
        "competition_level", "status", "source_urls",
    }
    # Listing truncates source URLs to five.
    assert len(top["source_urls"]) == 5
    assert top["status"] == "NEW"

    by_kind = intelligence_client.get(
        "/intelligence/opportunities?kind=PRODUCT_OPPORTUNITY", headers=intelligence_headers,
    ).json()
    assert [o["id"] for o in by_kind["opportunities"]] == [product_opp.id]

    limited = intelligence_client.get("/intelligence/opportunities?limit=1", headers=intelligence_headers).json()
    assert [o["id"] for o in limited["opportunities"]] == [gap.id]


# --- GET /intelligence/opportunities/{id} --------------------------------------


def test_opportunity_detail_returns_the_full_row(intelligence_client, intelligence_headers, db_session):
    org = _org_id(db_session)
    row = _opportunity(org, type_="MARKET_GAP", score=75.0, urls=[f"https://s.example/{i}" for i in range(9)])
    db_session.add(row)
    db_session.commit()

    response = intelligence_client.get(f"/intelligence/opportunities/{row.id}", headers=intelligence_headers)
    assert response.status_code == 200
    body = response.json()
    # Detail keeps the full row: no URL truncation, evidence and action included.
    assert len(body["source_urls"]) == 9
    assert body["evidence"] == {"claims": ["claim"]}
    assert body["recommended_action"] == "act on it"
    assert body["score"] == 75.0
    assert body["status"] == "NEW"


def test_opportunity_detail_missing_is_404(intelligence_client, intelligence_headers):
    response = intelligence_client.get("/intelligence/opportunities/999999", headers=intelligence_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Opportunity not found"


def test_opportunity_detail_other_organization_is_404(intelligence_client, intelligence_headers, db_session):
    row = _opportunity(999, type_="MARKET_GAP", score=50.0)
    db_session.add(row)
    db_session.commit()

    response = intelligence_client.get(f"/intelligence/opportunities/{row.id}", headers=intelligence_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Opportunity not found"


@pytest.mark.parametrize("path", [
    "/intelligence/jobs",
    "/intelligence/stores",
    "/intelligence/opportunities",
])
def test_intelligence_routes_require_auth(intelligence_client, path):
    assert intelligence_client.get(path).status_code in {401, 403}

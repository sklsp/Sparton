"""The commerce REST surface that the product journey doesn't exercise.

tests/test_ecommerce.py walks the loop end to end (add shop -> crawl -> change
-> report) and pins tenant isolation on every read path; this file covers what
the API owes the customer at the edges of that journey, where a regression is
silent:

* plan limits are enforced on the mutation routes themselves, not only in the
  billing service (which tests/test_billing.py unit-tests);
* the watchlist and single-competitor crawl endpoints behave like their docs;
* report listings omit the markdown (it is large) while details keep it;
* the alert inbox filters actually filter.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database.domain_models import Job
from app.core.database.ecommerce_models import (
    ChangeEvent,
    ChangeKind,
    ChangeSeverity,
    Competitor,
    CompetitorProduct,
    ProductMatch,
    Shop,
    ShopProduct,
)
from app.core.database.models import utcnow
from app.core.database.usage_models import LLMUsage

ORG_URL = "https://acme-homeware.myshopify.com"
RIVAL_URL = "https://claybarn-co.myshopify.com"
MUG_URL = f"{RIVAL_URL}/products/matte-ceramic-mug"


# ---------------------------------------------------------------------------
# Fixtures and seeding helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Same as test_ecommerce.py: make fixture hostnames resolve to a public IP.

    The SSRF guard resolves DNS before any shop or competitor URL is accepted,
    and these tests have no network.
    """
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )


@pytest.fixture()
def org_id(client, auth_headers, db_session) -> int:
    from app.core.database.identity import User

    user = db_session.execute(
        select(User).where(User.email == "admin@example.com")
    ).scalars().first()
    assert user is not None and user.organization_id is not None
    return user.organization_id


@pytest.fixture()
def shop(client, auth_headers) -> dict:
    response = client.post("/shops", headers=auth_headers, json={
        "url": ORG_URL, "name": "Acme Homeware", "category": "homeware"})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def rival(client, auth_headers, shop) -> dict:
    response = client.post("/competitors", headers=auth_headers, json={
        "url": RIVAL_URL, "name": "Claybarn Co", "shop_id": shop["id"]})
    assert response.status_code == 201, response.text
    return response.json()


def seed_change(db, org_id: int, shop_id: int, competitor_id: int | None = None, **overrides):
    base = dict(
        organization_id=org_id, shop_id=shop_id, competitor_id=competitor_id,
        kind=ChangeKind.PRICE_DECREASE, severity=ChangeSeverity.HIGH,
        product_name="Matte Ceramic Mug", competitor_name="Claybarn Co",
        previous_price=24.0, new_price=19.9, delta=-4.1, delta_pct=-17.1,
        currency="EUR", title="Claybarn Co cut the price", evidence_url=MUG_URL,
    )
    base.update(overrides)
    row = ChangeEvent(**base)
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# Plan limits on the mutation routes
# ---------------------------------------------------------------------------
class TestPlanLimits:
    """Free plan: one shop, three competitors, weekly crawls.

    The service functions are unit-tested in tests/test_billing.py; what is not
    tested anywhere else is that these routes actually call them and turn a
    PlanLimitError into the 402 the UI keys off of.
    """

    def test_second_shop_is_rejected_with_402(self, client, auth_headers, shop):
        response = client.post(
            "/shops", headers=auth_headers, json={"url": "https://second-store.myshopify.com"})
        assert response.status_code == 402
        body = response.json()["detail"]
        assert body["error"] == "plan_limit_reached"

    def test_fourth_competitor_is_rejected_with_402(self, client, auth_headers):
        for i in range(1, 4):
            ok = client.post("/competitors", headers=auth_headers, json={
                "url": f"https://rival-{i}.example.com"})
            assert ok.status_code == 201, ok.text

        blocked = client.post("/competitors", headers=auth_headers, json={
            "url": "https://rival-4.example.com"})
        assert blocked.status_code == 402
        assert blocked.json()["detail"]["error"] == "plan_limit_reached"

    def test_free_plan_cannot_buy_faster_crawls(self, client, auth_headers, shop):
        """Slower is kept (test_ecommerce.py); faster is clamped to the plan floor."""
        response = client.patch(
            f"/shops/{shop['id']}", headers=auth_headers, json={"crawl_frequency_hours": 24})
        assert response.status_code == 200
        # Free crawls weekly: a daily cadence is not something the plan grants.
        assert response.json()["crawl_frequency_hours"] == 168


class TestCreateShopErrors:
    """POST /shops rejects bad input before it can be stored or crawled."""

    def test_invalid_url_is_400(self, client, auth_headers):
        # The scheme check fires before any DNS lookup, so this is deterministic.
        response = client.post("/shops", headers=auth_headers, json={"url": "ftp://example.com"})
        assert response.status_code == 400
        assert response.json()["detail"] == "Only http and https URLs are supported"

    def test_readding_the_same_shop_is_409(self, client, auth_headers, shop):
        # De-duplication runs before the plan check: a customer re-adding their
        # own shop is told "you already added it", not asked to upgrade.
        response = client.post("/shops", headers=auth_headers, json={"url": ORG_URL})
        assert response.status_code == 409
        assert response.json()["detail"] == "You have already added this shop"


class TestCreateCompetitorErrors:
    """POST /competitors maps every ShopError to the status it declares."""

    def test_invalid_url_is_400(self, client, auth_headers):
        response = client.post(
            "/competitors", headers=auth_headers, json={"url": "ftp://rival.example.com"})
        assert response.status_code == 400
        assert response.json()["detail"] == "Only http and https URLs are supported"

    def test_own_shop_url_is_rejected_with_400(self, client, auth_headers, shop):
        # The self-check only applies when a shop_id is given: without it there
        # is no owner to compare the domain against.
        response = client.post(
            "/competitors", headers=auth_headers,
            json={"url": ORG_URL, "shop_id": shop["id"]})
        assert response.status_code == 400
        assert response.json()["detail"] == "That is your own shop, not a competitor"

    def test_unknown_shop_id_is_404(self, client, auth_headers):
        # 404 rather than 403: the API must not confirm that another tenant's
        # shop exists.
        response = client.post(
            "/competitors", headers=auth_headers,
            json={"url": RIVAL_URL, "shop_id": 999999})
        assert response.status_code == 404
        assert response.json()["detail"] == "Shop not found"

    def test_readding_a_tracked_competitor_is_409(self, client, auth_headers, rival):
        response = client.post("/competitors", headers=auth_headers, json={"url": RIVAL_URL})
        assert response.status_code == 409
        assert response.json()["detail"] == "You are already tracking this competitor"


# ---------------------------------------------------------------------------
# The watchlist
# ---------------------------------------------------------------------------
class TestWatchlist:
    def test_list_competitors(self, client, auth_headers, shop, rival):
        body = client.get("/competitors", headers=auth_headers).json()
        assert body["count"] == 1
        row = body["competitors"][0]
        assert row["domain"] == "claybarn-co.myshopify.com"
        assert row["is_active"] is True
        assert row["product_count"] == 0

    def test_list_competitors_requires_auth(self, client):
        assert client.get("/competitors").status_code in {401, 403}


# ---------------------------------------------------------------------------
# Crawling one competitor on demand
# ---------------------------------------------------------------------------
class TestSingleCompetitorCrawl:
    def test_crawl_one_competitor_enqueues_a_scoped_job(
        self, client, auth_headers, shop, rival, db_session
    ):
        response = client.post(f"/competitors/{rival['id']}/crawl", headers=auth_headers)
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["created"] is True

        job = db_session.get(Job, body["job_id"])
        assert job.type == "crawl_competitor"
        assert job.payload["competitor_id"] == rival["id"]
        assert job.payload["shop_id"] == shop["id"]
        assert job.organization_id is not None

    def test_crawl_unknown_competitor_is_404(self, client, auth_headers):
        response = client.post("/competitors/999999/crawl", headers=auth_headers)
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Report listings vs details
# ---------------------------------------------------------------------------
class TestReportListings:
    def test_listings_omit_markdown_but_details_keep_it(
        self, client, auth_headers, shop, rival, db_session, org_id
    ):
        from app.ecommerce.reports import generate_report

        seed_change(db_session, org_id, shop["id"], rival["id"])
        report = generate_report(
            db_session, organization_id=org_id,
            shop=db_session.get(Shop, shop["id"]), use_llm=False,
        )

        per_shop = client.get(f"/shops/{shop['id']}/reports", headers=auth_headers).json()
        assert per_shop["count"] == 1
        listed = per_shop["reports"][0]
        assert "markdown" not in listed
        assert listed["status"] == "COMPLETED"

        global_list = client.get("/reports", headers=auth_headers).json()
        assert global_list["count"] == 1
        assert "markdown" not in global_list["reports"][0]

        detail = client.get(f"/reports/{report.id}", headers=auth_headers).json()
        assert detail["markdown"]


# ---------------------------------------------------------------------------
# The alert inbox: filters and acknowledgement
# ---------------------------------------------------------------------------
class TestInboxFilters:
    def test_filter_by_severity(self, client, auth_headers, shop, rival, db_session, org_id):
        seed_change(db_session, org_id, shop["id"], rival["id"])
        seed_change(
            db_session, org_id, shop["id"], rival["id"],
            kind=ChangeKind.NEW_PRODUCT, severity=ChangeSeverity.LOW,
            product_name="Speckled Dinner Plate", title="New listing spotted",
        )

        body = client.get("/changes?severity=high", headers=auth_headers).json()
        assert body["count"] == 1
        assert body["changes"][0]["kind"] == "price_decrease"

    def test_days_window_excludes_old_changes(
        self, client, auth_headers, shop, rival, db_session, org_id
    ):
        seed_change(db_session, org_id, shop["id"], rival["id"],
                    detected_at=utcnow() - timedelta(days=40))
        recent = seed_change(
            db_session, org_id, shop["id"], rival["id"],
            product_name="Speckled Dinner Plate", title="Recent cut")

        body = client.get("/changes?days=30", headers=auth_headers).json()
        assert body["count"] == 1
        assert body["changes"][0]["id"] == recent.id

    def test_filter_by_shop(self, client, auth_headers, shop, db_session, org_id):
        """The free plan allows one shop via the API; seed a second directly so
        the filter itself can be tested without billing in the way."""
        other = Shop(
            organization_id=org_id, name="Acme Outlet", url="https://outlet.example.com/",
            domain="outlet.example.com")
        db_session.add(other)
        db_session.commit()

        seed_change(db_session, org_id, shop["id"], None, title="Cut at the main store")
        other_cut = seed_change(
            db_session, org_id, other.id, None, product_name="Speckled Dinner Plate",
            title="Cut at the outlet")

        body = client.get(f"/changes?shop_id={other.id}", headers=auth_headers).json()
        assert body["count"] == 1
        assert body["changes"][0]["id"] == other_cut.id

    def test_filter_by_kind(self, client, auth_headers, shop, rival, db_session, org_id):
        seed_change(db_session, org_id, shop["id"], rival["id"])
        new_listing = seed_change(
            db_session, org_id, shop["id"], rival["id"],
            kind=ChangeKind.NEW_PRODUCT, severity=ChangeSeverity.LOW,
            product_name="Speckled Dinner Plate", title="New listing spotted")

        body = client.get("/changes?kind=new_product", headers=auth_headers).json()
        assert body["count"] == 1
        assert body["changes"][0]["id"] == new_listing.id

    def test_unacknowledged_only_hides_read_alerts(
        self, client, auth_headers, shop, rival, db_session, org_id
    ):
        read = seed_change(db_session, org_id, shop["id"], rival["id"])
        still_open = seed_change(
            db_session, org_id, shop["id"], rival["id"],
            product_name="Speckled Dinner Plate", title="Still unread")
        client.post(f"/changes/{read.id}/ack", headers=auth_headers)

        body = client.get("/changes?unacknowledged_only=true", headers=auth_headers).json()
        assert body["count"] == 1
        assert body["changes"][0]["id"] == still_open.id


class TestAcknowledge:
    def test_re_ack_returns_the_original_timestamp(
        self, client, auth_headers, shop, db_session, org_id
    ):
        row = seed_change(db_session, org_id, shop["id"], None)

        first = client.post(f"/changes/{row.id}/ack", headers=auth_headers).json()
        second = client.post(f"/changes/{row.id}/ack", headers=auth_headers).json()
        assert first["acknowledged_at"] is not None
        # Compare instants, not strings: the first response serializes the
        # in-memory aware datetime (with +00:00), while a value read back from
        # SQLite comes out naive. Same instant, different rendering.
        assert _as_utc(first["acknowledged_at"]) == _as_utc(second["acknowledged_at"])

    def test_ack_unknown_change_is_404(self, client, auth_headers):
        response = client.post("/changes/999999/ack", headers=auth_headers)
        assert response.status_code == 404
        assert response.json()["detail"] == "Change not found"


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Shop list/delete, competitor shop filter, crawl-now and report-now routes
# ---------------------------------------------------------------------------
class TestShopListAndDelete:
    def test_list_shops(self, client, auth_headers, shop):
        body = client.get("/shops", headers=auth_headers).json()
        assert body["count"] == 1
        row = body["shops"][0]
        assert row["id"] == shop["id"]
        assert row["domain"] == "acme-homeware.myshopify.com"

    def test_delete_shop_removes_it_and_its_competitors(
        self, client, auth_headers, shop, rival, db_session
    ):
        response = client.delete(f"/shops/{shop['id']}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"deleted": True}

        body = client.get("/shops", headers=auth_headers).json()
        assert body["count"] == 0
        # The watchlist cascades with the shop.
        left = db_session.execute(
            select(Competitor).where(Competitor.shop_id == shop["id"])
        ).scalars().all()
        assert left == []

    def test_delete_unknown_shop_is_404(self, client, auth_headers):
        response = client.delete("/shops/999999", headers=auth_headers)
        assert response.status_code == 404


class TestCompetitorShopFilter:
    def test_filter_by_shop(self, client, auth_headers, shop, db_session, org_id):
        # A second shop seeded directly: the free plan allows one via API.
        other = Shop(
            organization_id=org_id, name="Acme Outlet",
            url="https://outlet.example.com/", domain="outlet.example.com")
        db_session.add(other)
        db_session.commit()

        main = client.post("/competitors", headers=auth_headers, json={
            "url": RIVAL_URL, "name": "Claybarn Co", "shop_id": shop["id"]})
        assert main.status_code == 201
        outlet = client.post("/competitors", headers=auth_headers, json={
            "url": "https://northlight-goods.myshopify.com",
            "name": "Northlight Goods", "shop_id": other.id})
        assert outlet.status_code == 201

        body = client.get(f"/competitors?shop_id={other.id}", headers=auth_headers).json()
        assert body["count"] == 1
        assert body["competitors"][0]["id"] == outlet.json()["id"]

        all_body = client.get("/competitors", headers=auth_headers).json()
        assert all_body["count"] == 2


# ---------------------------------------------------------------------------
# Shop service errors and the remaining update/delete/discover branches
# ---------------------------------------------------------------------------
class TestShopServiceErrors:
    def test_service_shop_error_maps_to_its_status(self, client, auth_headers, monkeypatch):
        """The route's except-branch: a ShopError raised by add_shop (after the
        route-level URL and duplicate checks) surfaces with its own status code
        and message instead of a 500."""
        from app.api import commerce as commerce_api
        from app.ecommerce.discovery import ShopError

        def boom(db, org_id, **kwargs):
            raise ShopError("That domain is reserved", status_code=409)

        monkeypatch.setattr(commerce_api, "add_shop", boom)
        response = client.post(
            "/shops", headers=auth_headers, json={"url": "https://reserved.example.com"})
        assert response.status_code == 409
        assert response.json()["detail"] == "That domain is reserved"


class TestCrawlShopNow:
    def test_crawl_without_competitors_is_400(self, client, auth_headers, shop):
        response = client.post(f"/shops/{shop['id']}/crawl", headers=auth_headers)
        assert response.status_code == 400
        assert response.json()["detail"] == "Add at least one competitor before crawling"

    def test_crawl_queues_one_job_per_day(self, client, auth_headers, shop, rival, db_session):
        first = client.post(f"/shops/{shop['id']}/crawl", headers=auth_headers)
        assert first.status_code == 202, first.text
        body = first.json()
        assert body["created"] is True
        assert body["competitors"] == 1

        job = db_session.get(Job, body["job_id"])
        assert job.type == "crawl_shop"
        assert job.payload["shop_id"] == shop["id"]
        assert job.payload["competitor_ids"] == [rival["id"]]

        # Same day: the per-day idempotency key returns the same job.
        again = client.post(f"/shops/{shop['id']}/crawl", headers=auth_headers)
        assert again.status_code == 202
        body2 = again.json()
        assert body2["created"] is False
        assert body2["job_id"] == body["job_id"]

    def test_crawl_unknown_shop_is_404(self, client, auth_headers):
        response = client.post("/shops/999999/crawl", headers=auth_headers)
        assert response.status_code == 404


class TestGenerateReportNow:
    def test_report_queues_a_job(self, client, auth_headers, shop, db_session):
        response = client.post(f"/shops/{shop['id']}/report?days=14", headers=auth_headers)
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["created"] is True

        job = db_session.get(Job, body["job_id"])
        assert job.type == "generate_report"
        assert job.payload == {"shop_id": shop["id"], "days": 14, "kind": "manual"}

    def test_daily_token_limit_blocks_the_route(
        self, client, auth_headers, shop, db_session, org_id
    ):
        # Free plan: 2000 tokens/day. One row over the limit is enough.
        db_session.add(LLMUsage(
            organization_id=org_id, provider="openai", model="gpt-4o-mini",
            task="report", total_tokens=2500))
        db_session.commit()

        response = client.post(f"/shops/{shop['id']}/report", headers=auth_headers)
        assert response.status_code == 402
        assert response.json()["detail"]["error"] == "plan_limit_reached"

    def test_report_unknown_shop_is_404(self, client, auth_headers):
        response = client.post("/shops/999999/report", headers=auth_headers)
        assert response.status_code == 404



class TestUpdateShop:
    def test_patch_updates_name_category_and_active_flag(self, client, auth_headers, shop):
        response = client.patch(
            f"/shops/{shop['id']}", headers=auth_headers,
            json={"name": "Acme Homeware GmbH", "category": "kitchen", "is_active": False})
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Acme Homeware GmbH"
        assert body["category"] == "kitchen"
        assert body["is_active"] is False

    def test_patch_unknown_shop_is_404(self, client, auth_headers):
        response = client.patch("/shops/999999", headers=auth_headers, json={"name": "Ghost"})
        assert response.status_code == 404


class TestDeleteCompetitor:
    def test_delete_competitor_removes_it(self, client, auth_headers, shop, rival):
        response = client.delete(f"/competitors/{rival['id']}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"deleted": True}
        body = client.get("/competitors", headers=auth_headers).json()
        assert body["count"] == 0

    def test_delete_unknown_competitor_is_404(self, client, auth_headers):
        assert client.delete("/competitors/999999", headers=auth_headers).status_code == 404


class TestDiscover:
    """POST /shops/{id}/discover proposes candidates; nothing is stored until the
    customer confirms. The search provider is faked at the route boundary, the
    same way test_ecommerce.py fakes it for suggest_competitors itself."""

    def test_candidates_pass_through_and_are_not_stored(
        self, client, auth_headers, shop, monkeypatch
    ):
        from app.api import commerce as commerce_api

        candidates = [
            {"domain": "rival-one.example.com", "url": "https://rival-one.example.com/",
             "platform": None, "reason": "Matches 'homeware Acme Homeware'", "confidence": 0.4},
            {"domain": "rival-two.example.com", "url": "https://rival-two.example.com/",
             "platform": None, "reason": "Matches 'homeware Acme Homeware'", "confidence": 0.4},
        ]
        monkeypatch.setattr(
            commerce_api, "suggest_competitors", lambda db, org_id, s: candidates)

        response = client.post(f"/shops/{shop['id']}/discover", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 2
        assert body["suggestions"] == candidates
        assert "not crawl" in body["message"].lower()

        # A proposal is not a confirmation: the watchlist stays empty.
        assert client.get("/competitors", headers=auth_headers).json()["count"] == 0

    def test_discover_unknown_shop_is_404(self, client, auth_headers):
        assert client.post("/shops/999999/discover", headers=auth_headers).status_code == 404


class TestTenantIsolationOnMutations:
    """test_ecommerce.py pins isolation on the read paths; these are the mutation
    routes, where a leak would mean one tenant editing or deleting another's data.
    The answer must be 404 (no confirmation that the other tenant's row exists)."""

    @staticmethod
    def _second_user(client) -> dict[str, str]:
        response = client.post("/auth/register", json={
            "email": "other@example.com", "password": "correct-horse-battery",
            "organization_name": "Other Org"})
        assert response.status_code == 201, response.text
        return {"Authorization": f"Bearer {response.json()['token']}"}

    def test_other_orgs_shop_is_404_on_every_route(self, client, auth_headers, shop):
        other = self._second_user(client)
        assert client.get(f"/shops/{shop['id']}", headers=other).status_code == 404
        assert client.patch(
            f"/shops/{shop['id']}", headers=other, json={"name": "Stolen"}).status_code == 404
        assert client.delete(f"/shops/{shop['id']}", headers=other).status_code == 404

        # The refusals changed nothing: the owner still sees their shop.
        assert client.get(f"/shops/{shop['id']}", headers=auth_headers).status_code == 200

    def test_other_orgs_competitor_is_404(self, client, auth_headers, shop, rival):
        other = self._second_user(client)
        assert client.delete(
            f"/competitors/{rival['id']}", headers=other).status_code == 404

        # The 404 was a refusal, not a delete.
        assert client.get("/competitors", headers=auth_headers).json()["count"] == 1


# ---------------------------------------------------------------------------
# Change history: the evidence trail behind a detected change
# ---------------------------------------------------------------------------
class TestChangeHistory:
    def test_unknown_change_is_404(self, client, auth_headers):
        response = client.get("/changes/999999/history", headers=auth_headers)
        assert response.status_code == 404

    def test_points_are_oldest_first_with_their_source(
        self, client, auth_headers, shop, rival, db_session, org_id
    ):
        now = utcnow()
        for hours_ago, price in ((48, "24.0"), (24, "18.5")):
            db_session.add(CompetitorProduct(
                organization_id=org_id, competitor_id=rival["id"], source_url=MUG_URL,
                name="Matte Ceramic Mug", normalized_name="matte ceramic mug",
                price=Decimal(price), currency="EUR", in_stock=True,
                data_source="jsonld", captured_at=now - timedelta(hours=hours_ago)))
        # The anchor capture: the reading the change was detected from.
        anchor = CompetitorProduct(
            organization_id=org_id, competitor_id=rival["id"], source_url=MUG_URL,
            name="Matte Ceramic Mug", normalized_name="matte ceramic mug",
            price=Decimal("19.9"), currency="EUR", in_stock=True,
            data_source="jsonld", captured_at=now)
        db_session.add(anchor)
        db_session.commit()

        change = seed_change(
            db_session, org_id, shop["id"], rival["id"], competitor_product_id=anchor.id)

        body = client.get(f"/changes/{change.id}/history", headers=auth_headers).json()
        assert [float(p["price"]) for p in body["points"]] == [24.0, 18.5, 19.9]
        stamps = [p["captured_at"] for p in body["points"]]
        assert stamps == sorted(stamps)
        assert all(p["source_url"] == MUG_URL for p in body["points"])
        assert "change" in body

    def test_falls_back_to_normalized_name_when_urls_rewrite(
        self, client, auth_headers, shop, rival, db_session, org_id
    ):
        # One capture under a rewritten URL: the same product, new slug. The
        # history must still find it via the normalized name.
        anchor = CompetitorProduct(
            organization_id=org_id, competitor_id=rival["id"], source_url=MUG_URL,
            name="Matte Ceramic Mug", normalized_name="matte ceramic mug",
            price=Decimal("19.9"), currency="EUR", in_stock=True,
            data_source="jsonld", captured_at=utcnow())
        db_session.add(anchor)
        db_session.add(CompetitorProduct(
            organization_id=org_id, competitor_id=rival["id"],
            source_url=f"{RIVAL_URL}/products/mug-v2",
            name="Matte Ceramic Mug", normalized_name="matte ceramic mug",
            price=Decimal("18.5"), currency="EUR", in_stock=True,
            data_source="jsonld", captured_at=utcnow() - timedelta(hours=24)))
        db_session.commit()

        change = seed_change(
            db_session, org_id, shop["id"], rival["id"], competitor_product_id=anchor.id)

        body = client.get(f"/changes/{change.id}/history", headers=auth_headers).json()
        assert [float(p["price"]) for p in body["points"]] == [18.5, 19.9]

    def test_own_product_history_when_matched(
        self, client, auth_headers, shop, rival, db_session, org_id
    ):
        own_url = f"{ORG_URL}/products/matte-ceramic-mug"
        anchor = CompetitorProduct(
            organization_id=org_id, competitor_id=rival["id"], source_url=MUG_URL,
            name="Matte Ceramic Mug", normalized_name="matte ceramic mug",
            price=Decimal("19.9"), currency="EUR", in_stock=True,
            data_source="jsonld", captured_at=utcnow())
        db_session.add(anchor)
        match = ProductMatch(
            organization_id=org_id, shop_id=shop["id"], competitor_id=rival["id"],
            own_url=own_url, own_name="Matte Ceramic Mug (Ours)",
            competitor_url=MUG_URL, method="gtin", score=1.0)
        db_session.add(match)
        for hours_ago, price in ((24, "26.0"), (0, "25.0")):
            db_session.add(ShopProduct(
                organization_id=org_id, shop_id=shop["id"], source_url=own_url,
                name="Matte Ceramic Mug (Ours)",
                price=Decimal(price), currency="EUR", in_stock=True,
                data_source="jsonld", captured_at=utcnow() - timedelta(hours=hours_ago)))
        db_session.commit()

        change = seed_change(
            db_session, org_id, shop["id"], rival["id"], competitor_product_id=anchor.id)

        body = client.get(f"/changes/{change.id}/history", headers=auth_headers).json()
        assert body["own"]["product"] == "Matte Ceramic Mug (Ours)"
        assert [float(p["price"]) for p in body["own"]["points"]] == [26.0, 25.0]


# ---------------------------------------------------------------------------
# Report detail 404 and the dashboard overview
# ---------------------------------------------------------------------------
class TestReportDetail:
    def test_unknown_report_is_404(self, client, auth_headers):
        response = client.get("/reports/999999", headers=auth_headers)
        assert response.status_code == 404
        assert response.json()["detail"] == "Report not found"


class TestOverview:
    def test_overview_shape(self, client, auth_headers, shop, rival, db_session, org_id):
        from app.ecommerce.reports import generate_report

        seed_change(db_session, org_id, shop["id"], rival["id"])
        report = generate_report(
            db_session, organization_id=org_id,
            shop=db_session.get(Shop, shop["id"]), use_llm=False)

        body = client.get("/overview", headers=auth_headers).json()
        assert len(body["shops"]) == 1
        assert body["competitor_count"] == 1
        assert body["changes_this_week"] >= 1
        assert body["unacknowledged"] >= 1
        assert body["competitors_healthy"] == 0
        assert body["latest_report"]["id"] == report.id


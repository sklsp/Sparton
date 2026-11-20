"""Branch coverage for app/ecommerce/crawl.py.

test_ecommerce.py walks the crawl loop's happy path end to end; this file pins
the branches that path never touches: products without a URL, an inactive
competitor, the feed tier winning a crawl outright, an unexpected exception in
the crawler, and a commit failure at the very last moment. Each test maps to a
branch of crawl_competitor or its helpers, so the file doubles as documentation
of what happens when a competitor misbehaves.
"""

from __future__ import annotations

import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.core.database.ecommerce_models import (
    Competitor,
    CompetitorProduct,
    CrawlStatus,
)
from app.ecommerce.crawl import (
    CrawlOutcome,
    as_money,
    crawl_competitor,
    dedupe_products,
)
from app.research.extraction import ExtractedProduct

ORG_URL = "https://acme-homeware.myshopify.com"
RIVAL_URL = "https://claybarn-co.myshopify.com"


# ---------------------------------------------------------------------------
# Fixtures (module-local, same shape as test_ecommerce.py's)
# ---------------------------------------------------------------------------
@pytest.fixture()
def org_id(client, auth_headers, db_session):
    from app.core.database.identity import User

    return (
        db_session.execute(select(User).where(User.email == "admin@example.com"))
        .scalars()
        .first()
        .organization_id
    )


@pytest.fixture()
def shop(db_session, client, auth_headers, org_id):
    response = client.post("/shops", headers=auth_headers, json={"url": ORG_URL})
    assert response.status_code in (200, 201), response.text
    return {"id": response.json()["id"], "url": ORG_URL}


@pytest.fixture()
def rival(db_session, client, auth_headers, shop):
    response = client.post(
        "/competitors",
        headers=auth_headers,
        json={"url": RIVAL_URL, "shop_id": shop["id"]},
    )
    assert response.status_code in (200, 201), response.text
    return {"id": response.json()["id"], "url": RIVAL_URL}


def _allow_private(monkeypatch) -> None:
    """Let the SSRF gate pass without touching DNS: hermetic crawl tests."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "crawler_allow_private_addresses", True)


def no_feed_transport() -> httpx.MockTransport:
    """Nothing at all: no feed, so the crawl falls through to the HTML tier."""

    def handler(request: httpx.Request) -> httpx.Response:
        if urlparse(str(request.url)).path.startswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def shopify_feed_transport() -> httpx.MockTransport:
    """A fake Shopify feed: robots.txt, the currency probe, one page of products.

    Same shape as test_feeds.py's fixture, kept local so this file stands alone.
    Two products with awkward prices (24.99, 0.45) because a float cannot hold
    0.45 -- if the feed tier rounded anything, it would show up here.
    """
    products = [
        {
            "id": 101,
            "title": "Cast Iron Skillet",
            "handle": "cast-iron-skillet",
            "vendor": "Acme",
            "variants": [{"price": "24.99", "available": True, "sku": "SKU-101"}],
        },
        {
            "id": 102,
            "title": "Copper Kettle",
            "handle": "copper-kettle",
            "vendor": "Acme",
            "variants": [{"price": "0.45", "available": False, "sku": "SKU-102"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        if parsed.path.startswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if parsed.path.startswith("/cart.js"):
            return httpx.Response(200, json={"currency": "EUR"}, request=request)
        if parsed.path.startswith("/products.json"):
            # Shopify paginates; past the last page it returns []. Without that
            # the reader would ask for page 2, 3, ... forever.
            page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            body = {"products": products if page == 1 else []}
            return httpx.Response(200, json=body, request=request)
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)

# ---------------------------------------------------------------------------
# Pure helpers: as_money and dedupe_products
# ---------------------------------------------------------------------------
class TestAsMoney:
    def test_it_rejects_garbage(self):
        # "not a price" raises InvalidOperation inside Decimal(); the branch
        # must swallow it, because one bad page must not kill a crawl.
        assert as_money("not a price") is None

    def test_it_accepts_numbers_and_quantizes_them(self):
        assert as_money(12) == Decimal("12.0000")
        assert as_money("24.99") == Decimal("24.99")

    def test_it_rejects_non_finite_amounts(self):
        # NaN and infinity compare wrong; a price that is not finite is no
        # price at all, so the branch returns None instead of storing it.
        assert as_money(float("nan")) is None
        assert as_money(Decimal("Infinity")) is None


class TestDedupeProducts:
    def test_it_keeps_products_without_a_url(self):
        # A product with no URL cannot be keyed, so it must survive dedupe
        # rather than being silently dropped (which would read as "removed").
        keyed = ExtractedProduct(
            name="Mug", price=Decimal("24"), url=RIVAL_URL + "/products/mug"
        )
        unkeyed = ExtractedProduct(name="Orphan", price=Decimal("9"))
        kept = dedupe_products([keyed, unkeyed])
        assert [p.name for p in kept] == ["Mug", "Orphan"]

    def test_it_prefers_the_observation_with_a_price(self):
        bare = ExtractedProduct(name="Mug", url=RIVAL_URL + "/products/mug")
        priced = ExtractedProduct(
            name="Mug", price=Decimal("5"), url=RIVAL_URL + "/products/mug"
        )
        kept = dedupe_products([bare, priced])
        assert len(kept) == 1
        assert kept[0].price is not None


class TestCrawlOutcome:
    def test_to_dict_serializes_every_field(self):
        outcome = CrawlOutcome(
            competitor_id=7,
            domain="claybarn-co.myshopify.com",
            status=CrawlStatus.COMPLETED,
            pages_fetched=3,
            captures_written=2,
            data_source="feed",
        )
        data = outcome.to_dict()
        assert data["competitor_id"] == 7
        assert data["domain"] == "claybarn-co.myshopify.com"
        assert data["status"] == CrawlStatus.COMPLETED
        assert data["pages_fetched"] == 3
        assert data["captures_written"] == 2
        assert data["data_source"] == "feed"
        # The customer-facing wording travels with the payload, so the UI can
        # say "exact prices from the shop's feed" instead of implying a guess.
        assert "feed" in data["source_label"]

# ---------------------------------------------------------------------------
# crawl_competitor branches
# ---------------------------------------------------------------------------
class TestCrawlCompetitorBranches:
    def test_an_inactive_competitor_is_skipped(self, db_session, shop, rival, monkeypatch):
        # The customer switched the competitor off; a scheduled run must not
        # fetch a single page for it.
        _allow_private(monkeypatch)
        competitor = db_session.get(Competitor, rival["id"])
        competitor.is_active = False
        db_session.commit()

        outcome = crawl_competitor(
            db_session,
            competitor,
            shop_id=shop["id"],
            feed_transport=no_feed_transport(),  # hermetic; never reached anyway
            sleep=lambda _: None,
        )

        assert outcome.status == CrawlStatus.COMPLETED
        assert outcome.skipped is True
        assert outcome.pages_fetched == 0
        assert outcome.captures_written == 0

    def test_the_feed_tier_wins_and_stamps_the_source(
        self, db_session, shop, rival, monkeypatch
    ):
        # When the competitor publishes a feed, the crawl must take it: exact
        # prices, one request per page, no HTML walk and no LLM. The winning
        # tier is stamped on the competitor so the UI can show which kind of
        # number the customer is looking at.
        from app.ecommerce.feeds import shopfeed_available

        if not shopfeed_available():  # the feed tier needs the optional shopfeed package (not in CI)
            pytest.skip("shopfeed is not installed, so there is no feed tier")
        _allow_private(monkeypatch)
        competitor = db_session.get(Competitor, rival["id"])

        outcome = crawl_competitor(
            db_session,
            competitor,
            shop_id=shop["id"],
            feed_transport=shopify_feed_transport(),
            sleep=lambda _: None,
        )

        assert outcome.status == CrawlStatus.COMPLETED
        assert outcome.data_source == "feed"
        assert outcome.pages_fetched == 1
        assert outcome.captures_written == 2
        assert outcome.products_found == 2

        db_session.refresh(competitor)
        assert competitor.last_source == "feed"

        # The prices are the shop's own numbers, exact to the stored quantum.
        rows = (
            db_session.execute(select(CompetitorProduct))
            .scalars()
            .all()
        )
        by_name = {row.name: row for row in rows}
        assert by_name["Cast Iron Skillet"].price == Decimal("24.99")
        assert by_name["Copper Kettle"].price == Decimal("0.45")

    def test_an_unexpected_error_fails_the_crawl_and_closes_the_crawler(
        self, db_session, shop, rival, monkeypatch
    ):
        # A competitor must never take the whole run down: an unexpected
        # exception is caught, recorded on the outcome, and the crawler this
        # call owns is still closed in the finally block.
        from app.ecommerce import crawl as crawl_mod

        _allow_private(monkeypatch)
        competitor = db_session.get(Competitor, rival["id"])

        closed: list[bool] = []

        class ExplodingCrawler:
            def __init__(self, *, policy):
                self.policy = policy

            def crawl(self, urls):
                raise RuntimeError("boom")

            def close(self):
                closed.append(True)

        monkeypatch.setattr(crawl_mod, "ResponsibleCrawler", ExplodingCrawler)

        outcome = crawl_competitor(
            db_session,
            competitor,
            shop_id=shop["id"],
            feed_transport=no_feed_transport(),  # falls through to the HTML tier
            sleep=lambda _: None,
        )

        assert outcome.status == CrawlStatus.FAILED
        assert "boom" in (outcome.error or "")
        assert closed == [True]  # finally ran even though the crawl blew up

        db_session.refresh(competitor)
        assert competitor.last_status == CrawlStatus.FAILED


class TestFinish:
    def test_a_failed_commit_is_swallowed_and_rolled_back(self, db_session, rival):
        # The last thing _finish does is commit; if that fails (disk full,
        # lock timeout), the exception must not escape into the scheduler.
        # The outcome is returned as-is so the caller can still report it.
        from app.ecommerce import crawl as crawl_mod

        competitor = db_session.get(Competitor, rival["id"])
        outcome = CrawlOutcome(
            competitor_id=competitor.id,
            domain=competitor.domain,
            status=CrawlStatus.COMPLETED,
        )

        calls: dict[str, int] = {"commit": 0, "rollback": 0}

        def failing_commit() -> None:
            calls["commit"] += 1
            raise RuntimeError("disk on fire")

        def recording_rollback() -> None:
            calls["rollback"] += 1

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db_session, "commit", failing_commit)
            mp.setattr(db_session, "rollback", recording_rollback)
            returned = crawl_mod._finish(
                db_session, competitor, outcome, time.monotonic()
            )

        assert returned is outcome
        assert calls == {"commit": 1, "rollback": 1}

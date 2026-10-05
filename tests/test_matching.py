"""Own catalogue + product matching (v1.1, closes D-031).

What the customer is told: "they are 10.0% cheaper than you". That sentence has
three parts that can each be wrong, and each is pinned here: the gap maths
(Decimal, sign, rounding), the pairing (barcode first, "200g" never "400g"),
and the moment both prices are from. Plus the honesty rule: only a barcode
match is ever called certain.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database.ecommerce_models import (
    ChangeEvent,
    Competitor,
    CompetitorProduct,
    MatchConfidence,
    ProductMatch,
    Shop,
    ShopProduct,
)
from app.core.database.models import utcnow
from app.ecommerce.feeds import shopfeed_available
from app.ecommerce.matching import comparisons, price_gap, read_own_catalog, rematch

needs_shopfeed = pytest.mark.skipif(not shopfeed_available(), reason="shopfeed is not installed")


class TestThePriceGap:
    def test_cheaper_is_positive_dearer_is_negative(self):
        assert price_gap(Decimal("24.95"), Decimal("22.45")) == Decimal("10.0")
        assert price_gap(Decimal("10"), Decimal("11")) == Decimal("-10.0")
        assert price_gap(Decimal("10"), Decimal("10")) == Decimal("0.0")

    def test_it_is_decimal_and_rounds_half_up(self):
        gap = price_gap(Decimal("0.45"), Decimal("0.40"))
        assert isinstance(gap, Decimal)
        assert gap == Decimal("11.1")  # 11.111...
        assert price_gap(Decimal("8"), Decimal("7.996")) == Decimal("0.1")  # 0.05 rounds up

    def test_nothing_to_say_is_none(self):
        assert price_gap(None, Decimal("1")) is None
        assert price_gap(Decimal("1"), None) is None
        assert price_gap(Decimal("0"), Decimal("1")) is None


class TestConfidence:
    def test_only_a_barcode_is_certain(self):
        assert MatchConfidence.of("gtin", 1.0) == "certain"
        assert MatchConfidence.of("title", 1.0) == "likely"
        assert MatchConfidence.of("title", 0.75) == "possible"


# --- fixtures -----------------------------------------------------------------


def _org(db):
    from app.core.database.identity import User

    return db.execute(select(User).where(User.email == "admin@example.com")).scalars().first().organization_id


@pytest.fixture()
def shop_and_rival(client, auth_headers, db_session):
    org = _org(db_session)
    shop = Shop(organization_id=org, name="Mine", url="https://shop.test", domain="shop.test")
    db_session.add(shop)
    db_session.flush()
    rival = Competitor(
        organization_id=org, shop_id=shop.id, domain="rival.test", url="https://shop.test", name="Rival"
    )
    db_session.add(rival)
    db_session.commit()
    return shop, rival


def _own(db, shop, name, price, *, gtin="", when=None, url=None):
    row = ShopProduct(
        organization_id=shop.organization_id, shop_id=shop.id, source_url=url or f"https://shop.test/{name}",
        name=name, price=Decimal(price), gtin=gtin, captured_at=when or utcnow(),
    )
    db.add(row)
    return row


def _theirs(db, rival, name, price, *, gtin="", when=None, url=None):
    row = CompetitorProduct(
        organization_id=rival.organization_id, competitor_id=rival.id,
        source_url=url or f"https://rival.test/{name}", name=name, normalized_name=name.lower(),
        price=Decimal(price), gtin=gtin, captured_at=when or utcnow(),
    )
    db.add(row)
    return row


# --- pairing and storage ------------------------------------------------------


@needs_shopfeed
class TestMatching:
    def test_barcode_first_and_200g_never_pairs_with_400g(self, db_session, shop_and_rival):
        shop, rival = shop_and_rival
        t = utcnow()
        _own(db_session, shop, "Soja geurkaars Vanille 200g", "24.95", when=t)
        _own(db_session, shop, "Soja geurkaars Vanille 400g", "39.95", when=t)
        _own(db_session, shop, "Blue mug", "8.00", gtin="8712345678906", when=t)
        _theirs(db_session, rival, "Soja geurkaars - Vanille (200 g)", "22.45", when=t)
        _theirs(db_session, rival, "Enamel cup, cobalt", "7.50", gtin="8712345678906", when=t)
        db_session.commit()

        assert rematch(db_session, shop) == 2
        found = {m.own_name: m for m in db_session.execute(select(ProductMatch)).scalars()}
        assert set(found) == {"Soja geurkaars Vanille 200g", "Blue mug"}
        assert found["Blue mug"].method == "gtin"
        assert found["Blue mug"].confidence == "certain"
        assert found["Soja geurkaars Vanille 200g"].method == "title"
        assert found["Soja geurkaars Vanille 200g"].confidence != "certain"

    def test_rematch_replaces_and_uses_only_the_latest_capture(self, db_session, shop_and_rival):
        shop, rival = shop_and_rival
        old, new = utcnow() - timedelta(days=7), utcnow()
        _own(db_session, shop, "Linen towel", "12.00", when=new)
        _theirs(db_session, rival, "Linen towel", "11.00", when=old)
        db_session.commit()
        assert rematch(db_session, shop) == 1
        # The rival's latest crawl no longer lists it: the match must go.
        _theirs(db_session, rival, "Wool blanket", "60.00", when=new)
        db_session.commit()
        assert rematch(db_session, shop) == 0
        assert db_session.execute(select(ProductMatch)).scalars().all() == []

    def test_own_catalogue_is_read_like_a_competitor_and_matched(self, db_session, shop_and_rival, llm):
        """End to end: the own shop and the rival both read from a (fake) Shopify feed."""
        from tests.test_feeds import TestTheCrawlPrefersTheShopFeed, _NoWait, shopify_transport

        shop, rival = shop_and_rival
        result = read_own_catalog(
            db_session, shop, transport=shopify_transport(), allow_private=True, sleep=_NoWait()
        )
        assert result == {"products": 2, "data_source": "feed", "error": ""}
        own = {p.name: p for p in db_session.execute(select(ShopProduct)).scalars()}
        assert own["Copper Kettle"].price == Decimal("0.4500")  # exact, not 0.45000000000000001
        assert own["Cast Iron Skillet"].gtin == "5012345678900"

        outcome = TestTheCrawlPrefersTheShopFeed()._crawl(db_session, rival, shopify_transport())
        assert outcome.status == "COMPLETED", outcome.error
        assert rematch(db_session, shop) == 2
        by = {m.own_name: m.confidence for m in db_session.execute(select(ProductMatch)).scalars()}
        assert by == {"Cast Iron Skillet": "certain", "Copper Kettle": "likely"}

    def test_an_unreadable_own_shop_writes_nothing(self, db_session, shop_and_rival):
        from tests.test_feeds import _NoWait, no_feed_transport

        shop, _ = shop_and_rival
        result = read_own_catalog(
            db_session, shop, transport=no_feed_transport(), allow_private=True, sleep=_NoWait()
        )
        assert result["products"] == 0 and result["error"]
        assert db_session.execute(select(ShopProduct)).scalars().all() == []


# --- the API ------------------------------------------------------------------


@pytest.fixture()
def seeded(db_session, shop_and_rival):
    """A matched price drop, an unmatched one, and a match owned by another tenant."""
    shop, rival = shop_and_rival
    t0, t1, t2 = utcnow() - timedelta(days=3), utcnow() - timedelta(days=2), utcnow() - timedelta(days=1)
    _own(db_session, shop, "Vanille 200g", "24.95", when=t0, url="https://shop.test/vanille")
    # Taken after the change: must not be what the change is compared with.
    _own(db_session, shop, "Vanille 200g", "30.00", when=t2, url="https://shop.test/vanille")
    capture = _theirs(db_session, rival, "Vanille (200 g)", "22.45", when=t1, url="https://rival.test/vanille")
    db_session.flush()

    def change(url, name):
        row = ChangeEvent(
            organization_id=shop.organization_id, shop_id=shop.id, competitor_id=rival.id,
            competitor_product_id=capture.id if url == capture.source_url else None, kind="price_decrease", product_name=name,
            source_url=url, evidence_url=url, competitor_name="Rival", previous_price=Decimal("24.95"),
            new_price=Decimal("22.45"), currency="EUR", title=name, detected_at=t1,
        )
        db_session.add(row)
        return row

    matched = change("https://rival.test/vanille", "Vanille (200 g)")
    unmatched = change("https://rival.test/other", "Other")
    foreign = change("https://rival.test/foreign", "Foreign")
    db_session.add(ProductMatch(
        organization_id=shop.organization_id, shop_id=shop.id, competitor_id=rival.id,
        own_url="https://shop.test/vanille", own_name="Vanille 200g",
        competitor_url="https://rival.test/vanille", competitor_name="Vanille (200 g)",
        method="title", score=0.8,
    ))
    db_session.add(ProductMatch(
        organization_id=(shop.organization_id or 0) + 999, shop_id=shop.id, competitor_id=rival.id,
        own_url="https://shop.test/vanille", own_name="Not yours",
        competitor_url="https://rival.test/foreign", competitor_name="Foreign", method="gtin", score=1.0,
    ))
    db_session.commit()
    return matched.id, unmatched.id, foreign.id


class TestTheApi:
    def test_changes_say_how_much_cheaper_they_are(self, client, auth_headers, seeded):
        matched, unmatched, foreign = seeded
        body = client.get("/changes", headers=auth_headers).json()
        by_id = {c["id"]: c for c in body["changes"]}
        vs = by_id[matched]["vs_you"]
        # (24.95 - 22.45) / 24.95, with the own price in force AT the change.
        # Money travels as exact decimal strings, like every price in this API.
        assert Decimal(vs["gap_pct"]) == Decimal("10.0")
        assert Decimal(vs["own_price"]) == Decimal("24.95")
        assert vs["confidence"] == "possible"  # a 0.8 title match is labelled, not certain
        assert by_id[unmatched]["vs_you"] is None
        assert by_id[foreign]["vs_you"] is None  # another tenant's match is invisible

    def test_the_overview_board_carries_it_too(self, client, auth_headers, seeded):
        matched, _, _ = seeded
        body = client.get("/overview", headers=auth_headers).json()
        row = next(c for c in body["recent_changes"] if c["id"] == matched)
        assert Decimal(row["vs_you"]["gap_pct"]) == Decimal("10.0")

    def test_history_has_your_price_line(self, client, auth_headers, seeded):
        matched, unmatched, _ = seeded
        body = client.get(f"/changes/{matched}/history", headers=auth_headers).json()
        assert body["own"]["confidence"] == "possible"
        assert [Decimal(p["price"]) for p in body["own"]["points"]] == [Decimal("24.95"), Decimal("30")]
        none = client.get(f"/changes/{unmatched}/history", headers=auth_headers).json()
        assert none["own"] is None


class TestTheCrawlJob:
    def test_a_broken_own_catalogue_does_not_stop_the_competitor_crawl(
        self, db_session, shop_and_rival, monkeypatch
    ):
        from app.core.database.domain_models import Job
        from app.core.jobs.queue import enqueue
        from app.ecommerce import crawl, jobs, matching

        shop, rival = shop_and_rival
        crawled = []

        def boom(*args, **kwargs):
            raise RuntimeError("own shop unreachable")

        def fake_crawl(db, competitor, **kwargs):
            crawled.append(competitor.id)
            return crawl.CrawlOutcome(competitor.id, competitor.domain, "COMPLETED")

        monkeypatch.setattr(matching, "read_own_catalog", boom)
        monkeypatch.setattr(crawl, "crawl_competitor", fake_crawl)
        job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": shop.id},
                         organization_id=shop.organization_id)
        jobs.crawl_shop({"job_id": job.id, "shop_id": shop.id})

        db_session.expire_all()
        done = db_session.get(Job, job.id)
        assert done.status == "COMPLETED", done.error
        assert crawled == [rival.id]
        assert done.result["own_products"] == 0

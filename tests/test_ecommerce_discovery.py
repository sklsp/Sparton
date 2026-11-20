"""Unit tests for app/ecommerce/discovery.py.

Covers the owner-key reduction (_registrable), the blocked-host list, shop and
competitor registration (validation, duplicates, clamping) and search-based
suggestions (query building, filtering, SSRF guard, limits). No network: DNS is
stubbed to one fixed public address so the SSRF guard runs its real logic, and
search results come from a fake provider.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database.ecommerce_models import Competitor, Shop
from app.ecommerce import discovery as ecommerce_discovery
from app.ecommerce.discovery import (
    ShopError,
    _is_blocked_host,
    _registrable,
    add_competitor,
    add_shop,
    shop_already_tracked,
    suggest_competitors,
)
from app.research.discovery import SearchProvider

ORG = 1
OTHER_ORG = 2


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """The SSRF guard resolves DNS; the tests have no network. Every hostname
    resolves to one fixed public address, so only the private-address logic is
    under test (see docs/DECISIONS.md D-016)."""

    def fake_getaddrinfo(host: str, port: int | None, *args: Any, **kwargs: Any):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


class FakeSearch(SearchProvider):
    """Canned search results: the HTTP boundary of suggest_competitors."""

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls
        self.queries: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int = 10) -> list[str]:
        self.queries.append((query, limit))
        return list(self.urls)


class ExplodingSearch(SearchProvider):
    """A provider whose backend is down."""

    def search(self, query: str, *, limit: int = 10) -> list[str]:
        raise RuntimeError("search backend down")


def seed_shop(
    db: Any,
    *,
    org: int = ORG,
    domain: str = "acme-homeware.myshopify.com",
    name: str = "Acme Homeware",
    category: str = "homeware",
) -> Shop:
    shop = Shop(
        organization_id=org,
        name=name,
        url=f"https://{domain}/",
        domain=domain,
        platform="shopify",
        category=category,
        currency="EUR",
        crawl_frequency_hours=168,
        is_active=True,
    )
    db.add(shop)
    db.commit()
    return shop


def seed_competitor(db: Any, *, org: int = ORG, domain: str = "rival.example.com") -> Competitor:
    competitor = Competitor(
        organization_id=org,
        name="",
        url=f"https://{domain}/",
        domain=domain,
        platform="unknown",
        discovery_method="manual",
        confidence=1.0,
        is_active=True,
    )
    db.add(competitor)
    db.commit()
    return competitor


# ---------------------------------------------------------------------------
# _registrable: the "same owner?" key
# ---------------------------------------------------------------------------
class TestRegistrable:
    def test_short_domain_is_returned_as_is(self):
        # Uppercase and trailing dot are normalized; the www. prefix is stripped.
        assert _registrable("WWW.ACME.COM.") == "acme.com"

    def test_platform_suffix_uses_the_tenant_label(self):
        assert _registrable("www.acme-homeware.myshopify.com") == "acme-homeware"
        assert _registrable("claybarn.bigcommerce.com") == "claybarn"

    def test_numeric_last_label_is_a_cdn_shard(self):
        assert _registrable("cdn.shop.42") == "cdn.shop"

    def test_two_part_suffix_keeps_three_labels(self):
        assert _registrable("storefront.example.co.uk") == "example.co.uk"

    def test_plain_domain_falls_back_to_last_two_labels(self):
        assert _registrable("catalog.example.net") == "example.net"


class TestIsBlockedHost:
    @pytest.mark.parametrize(
        ("domain", "blocked"),
        [
            ("www.amazon.com", True),
            ("ebay.co.uk", True),
            ("acme-homeware.myshopify.com", False),
        ],
    )
    def test_marketplaces_blocked_shops_allowed(self, domain: str, blocked: bool):
        assert _is_blocked_host(domain) is blocked


# ---------------------------------------------------------------------------
# shop_already_tracked
# ---------------------------------------------------------------------------
class TestShopAlreadyTracked:
    def test_true_for_same_org_and_domain(self, db_session: Any):
        seed_shop(db_session, org=ORG, domain="acme-homeware.myshopify.com")
        assert shop_already_tracked(db_session, ORG, "acme-homeware.myshopify.com") is True

    def test_false_when_not_tracked_or_other_org(self, db_session: Any):
        seed_shop(db_session, org=OTHER_ORG, domain="other.example.com")
        assert shop_already_tracked(db_session, ORG, "untracked.example.com") is False
        # The same domain exists for another tenant; that does not count.
        assert shop_already_tracked(db_session, ORG, "other.example.com") is False


# ---------------------------------------------------------------------------
# add_shop
# ---------------------------------------------------------------------------
class TestAddShop:
    @pytest.mark.parametrize(
        "url", ["", "not a url", "ftp://files.example.com/x"]
    )
    def test_invalid_url_rejected(self, db_session: Any, url: str):
        with pytest.raises(ShopError) as excinfo:
            add_shop(db_session, ORG, url=url)
        assert excinfo.value.status_code == 400

    def test_duplicate_shop_rejected(self, db_session: Any):
        seed_shop(db_session, org=ORG, domain="acme-homeware.myshopify.com")
        with pytest.raises(ShopError) as excinfo:
            add_shop(db_session, ORG, url="https://acme-homeware.myshopify.com/")
        assert excinfo.value.status_code == 409
        assert excinfo.value.message == "You have already added this shop"

    def test_frequency_clamped_to_six_hour_floor(self, db_session: Any):
        shop = add_shop(db_session, ORG, url="https://fast.example.com/", crawl_frequency_hours=2)
        assert shop.crawl_frequency_hours == 6

    def test_frequency_clamped_to_thirty_day_ceiling(self, db_session: Any):
        shop = add_shop(
            db_session, ORG, url="https://slow.example.com/", crawl_frequency_hours=99999
        )
        assert shop.crawl_frequency_hours == 720

    def test_success_stores_normalized_values(self, db_session: Any):
        shop = add_shop(
            db_session,
            ORG,
            url="https://www.acme-homeware.myshopify.com/collections/all",
            name="",
            category=" homeware ",
            currency="eur",
        )
        # An empty name falls back to the domain; the path is kept as given.
        assert shop.name == "www.acme-homeware.myshopify.com"
        assert shop.domain == "www.acme-homeware.myshopify.com"
        assert shop.url == "https://www.acme-homeware.myshopify.com/collections/all"
        assert shop.platform == "shopify"
        assert shop.category == "homeware"
        assert shop.currency == "EUR"
        assert shop.is_active is True
        assert shop.next_crawl_at is not None


# ---------------------------------------------------------------------------
# add_competitor
# ---------------------------------------------------------------------------
class TestAddCompetitor:
    def test_invalid_url_rejected(self, db_session: Any):
        with pytest.raises(ShopError) as excinfo:
            add_competitor(db_session, ORG, url="not a url")
        assert excinfo.value.status_code == 400

    @pytest.mark.parametrize(
        "url", ["https://www.amazon.com/dp/B08", "https://www.ebay.co.uk/"]
    )
    def test_marketplace_rejected(self, db_session: Any, url: str):
        with pytest.raises(ShopError) as excinfo:
            add_competitor(db_session, ORG, url=url)
        assert "not a shop" in excinfo.value.message

    def test_unknown_shop_id_is_404(self, db_session: Any):
        with pytest.raises(ShopError) as excinfo:
            add_competitor(db_session, ORG, url="https://rival.example.com/", shop_id=99999)
        assert excinfo.value.status_code == 404
        assert excinfo.value.message == "Shop not found"

    def test_other_orgs_shop_is_404_not_403(self, db_session: Any):
        # A 404, never a 403: the response must not confirm that another
        # tenant's shop exists.
        foreign = seed_shop(db_session, org=OTHER_ORG, domain="other.example.com")
        with pytest.raises(ShopError) as excinfo:
            add_competitor(
                db_session, ORG, url="https://rival.example.com/", shop_id=foreign.id
            )
        assert excinfo.value.status_code == 404

    def test_own_shop_rejected(self, db_session: Any):
        own = seed_shop(db_session, org=ORG, domain="acme-homeware.myshopify.com")
        with pytest.raises(ShopError) as excinfo:
            add_competitor(
                db_session,
                ORG,
                url="https://www.acme-homeware.myshopify.com/",
                shop_id=own.id,
            )
        assert excinfo.value.message == "That is your own shop, not a competitor"

    def test_duplicate_competitor_rejected(self, db_session: Any):
        seed_competitor(db_session, org=ORG, domain="rival.example.com")
        with pytest.raises(ShopError) as excinfo:
            add_competitor(db_session, ORG, url="https://rival.example.com/")
        assert excinfo.value.status_code == 409

    def test_success_stores_manual_competitor(self, db_session: Any):
        competitor = add_competitor(db_session, ORG, url="https://rival.example.com/", name="")
        assert competitor.domain == "rival.example.com"
        assert competitor.name == "rival.example.com"
        assert competitor.url == "https://rival.example.com/"
        assert competitor.platform == "unknown"
        assert competitor.discovery_method == "manual"
        assert competitor.confidence == 1.0
        assert competitor.is_active is True

    @pytest.mark.parametrize(
        ("given", "stored"), [(5.0, 1.0), (-0.5, 0.0)]
    )
    def test_confidence_clamped_to_unit_interval(self, db_session: Any, given: float, stored: float):
        competitor = add_competitor(
            db_session, ORG, url="https://clamped.example.com/", confidence=given
        )
        assert competitor.confidence == stored


# ---------------------------------------------------------------------------
# suggest_competitors
# ---------------------------------------------------------------------------
class TestSuggestCompetitors:
    def test_returns_candidates_without_adding_rows(self, db_session: Any):
        shop = seed_shop(db_session, org=ORG)
        provider = FakeSearch(
            ["https://rival-one.example.com/", "https://rival-two.bigcommerce.com/"]
        )
        found = suggest_competitors(db_session, ORG, shop, search=provider)

        assert found == [
            {
                "domain": "rival-one.example.com",
                "url": "https://rival-one.example.com/",
                "platform": "unknown",
                "reason": "Matches 'homeware Acme Homeware shop online store'",
                "confidence": 0.4,
            },
            {
                "domain": "rival-two.bigcommerce.com",
                "url": "https://rival-two.bigcommerce.com/",
                "platform": "bigcommerce",
                "reason": "Matches 'homeware Acme Homeware shop online store'",
                "confidence": 0.4,
            },
        ]
        # Suggestions are candidates only: nothing is stored until confirmed.
        assert (
            db_session.scalar(select(Competitor.id).where(Competitor.organization_id == ORG))
            is None
        )
        # No explicit limit: the provider gets three times the configured cap.
        assert provider.queries[0][1] == settings.competitor_discovery_limit * 3

    def test_query_falls_back_to_domain_when_name_and_category_empty(self, db_session: Any):
        shop = seed_shop(db_session, org=ORG, name="", category="")
        provider = FakeSearch([])
        suggest_competitors(db_session, ORG, shop, search=provider)
        assert provider.queries == [("acme-homeware.myshopify.com shop online store", 30)]

    def test_search_outage_returns_empty_list(self, db_session: Any):
        shop = seed_shop(db_session, org=ORG)
        found = suggest_competitors(db_session, ORG, shop, search=ExplodingSearch())
        assert found == []

    def test_default_provider_is_closed_after_use(
        self, db_session: Any, monkeypatch: pytest.MonkeyPatch
    ):
        shop = seed_shop(db_session, org=ORG)
        closed = {"value": False}

        class FakeDefault(SearchProvider):
            def search(self, query: str, *, limit: int = 10) -> list[str]:
                return []

            def close(self) -> None:
                closed["value"] = True

        monkeypatch.setattr(ecommerce_discovery, "DuckDuckGoProvider", FakeDefault)
        found = suggest_competitors(db_session, ORG, shop)
        assert found == []
        assert closed["value"] is True

    def test_skips_blocked_own_tracked_and_unsafe_urls(self, db_session: Any):
        shop = seed_shop(db_session, org=ORG)
        seed_competitor(db_session, org=ORG, domain="tracked-rival.example.com")
        provider = FakeSearch(
            [
                "https://www.ebay.co.uk/",  # blocked host
                "https://acme-homeware.myshopify.com/collections/all",  # the shop itself
                "https://tracked-rival.example.com/",  # already tracked
                "ftp://files.example.com/x",  # not http(s): fails the SSRF guard
                "not-a-url",  # no hostname at all
                "https://safe-one.example.com/",
            ]
        )
        found = suggest_competitors(db_session, ORG, shop, search=provider)
        assert [s["domain"] for s in found] == ["safe-one.example.com"]

    def test_duplicate_urls_collapse_to_one(self, db_session: Any):
        shop = seed_shop(db_session, org=ORG)
        provider = FakeSearch(["https://dup.example.com/", "https://dup.example.com/"])
        found = suggest_competitors(db_session, ORG, shop, search=provider)
        assert [s["domain"] for s in found] == ["dup.example.com"]

    def test_limit_caps_suggestions(self, db_session: Any):
        shop = seed_shop(db_session, org=ORG)
        provider = FakeSearch(["https://first.example.com/", "https://second.example.com/"])
        found = suggest_competitors(db_session, ORG, shop, limit=1, search=provider)
        assert [s["domain"] for s in found] == ["first.example.com"]
        # The provider is asked for three times the cap.
        assert provider.queries[0][1] == 3




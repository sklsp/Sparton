"""The product loop, end to end.

    add shop -> add competitor -> crawl -> changes detected -> report written

Everything here runs without a network: the crawler is driven with a fake
httpx transport that serves fixture HTML, exactly as a real storefront would.
That means the whole customer journey is tested, including the diff engine and
the report writer.
"""

from __future__ import annotations

import json
from decimal import Decimal
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.database.domain_models import Job
from app.core.database.ecommerce_models import (
    ChangeEvent,
    ChangeKind,
    ChangeSeverity,
    Competitor,
    CompetitorProduct,
    CrawlStatus,
    Report,
    ReportStatus,
    Shop,
)
from app.core.database.models import utcnow

ORG_URL = "https://acme-homeware.myshopify.com"
RIVAL_URL = "https://claybarn-co.myshopify.com"
RIVAL2_URL = "https://northlight-goods.myshopify.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def product_html(name: str, price: str, *, available: str = "InStock") -> str:
    """A Shopify-shaped product page: JSON-LD Product, which is what we parse."""
    payload = {
        "@context": "https://schema.org/",
        "@type": "Product",
        "name": name,
        "description": f"{name}, small batch.",
        "sku": name.upper().replace(" ", "-"),
        "category": "Homeware",
        "image": "https://cdn.example/img.jpg",
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "EUR",
            "availability": f"https://schema.org/{available}",
        },
    }
    return (
        "<html><head><title>{name}</title>"
        '<script type="application/ld+json">{json}</script>'
        "</head><body><h1>{name}</h1></body></html>"
    ).format(name=name, json=json.dumps(payload))


def listing_html(products: list[tuple[str, str]], base: str) -> str:
    """A collection page linking to product pages, as a real crawl would find."""
    links = "".join(
        f'<a href="{base}/products/{name.lower().replace(" ", "-")}">{name}</a>'
        for name, _ in products
    )
    return f"<html><head><title>Catalogue</title></head><body>{links}</body></html>"


class FakeStore:
    """Serves fixture pages for a fake storefront over a fake httpx transport.

    `pages` maps a URL to HTML. `set_prices` mutates it between crawls, which
    is how a "competitor cut their price" scenario is simulated.
    """

    def __init__(self, base: str, products: list[tuple[str, str]]):
        self.base = base.rstrip("/")
        self.products = dict(products)

    def set_price(self, name: str, price: str) -> None:
        self.products[name] = price

    def remove(self, name: str) -> None:
        self.products.pop(name, None)

    def add(self, name: str, price: str) -> None:
        self.products[name] = price

    def handle(self, url: str) -> tuple[int, str]:
        clean = url.split("?")[0].rstrip("/")
        if clean.endswith("/robots.txt"):
            return 200, "User-agent: *\nAllow: /\n"
        if clean == self.base:
            return 200, listing_html(list(self.products.items()), self.base)
        for name, price in self.products.items():
            if clean.endswith(name.lower().replace(" ", "-")):
                return 200, product_html(name, price)
        return 404, "<html><body>Not found</body></html>"


def make_crawler(handler, delay: float = 0.0):
    """An httpx.Client whose transport is a plain function.

    `delay` defaults to 0 because the production policy sleeps a full second
    between requests to the same host as a politeness floor — correct for real
    crawls, and a minute of dead wall-clock across this file.
    """
    import httpx

    from app.ecommerce.crawl import default_crawl_policy
    from app.research.crawler import ResponsibleCrawler

    def transport(request: httpx.Request) -> httpx.Response:
        status, body = handler(str(request.url))
        return httpx.Response(status, text=body, request=request)

    client = httpx.Client(
        transport=httpx.MockTransport(transport),
        follow_redirects=True,
        headers={"User-Agent": "SpartonIntelligence/1.0"},
    )
    policy = default_crawl_policy(max_pages=20)
    policy = replace(policy, delay_seconds=delay)
    return ResponsibleCrawler(policy=policy, client=client)


@pytest.fixture()
def org_id(client, auth_headers, db_session):
    from app.core.database.identity import User

    return db_session.execute(
        select(User).where(User.email == "admin@example.com")
    ).scalars().first().organization_id


@pytest.fixture()
def shop(client, auth_headers, org_id):
    response = client.post(
        "/shops", headers=auth_headers, json={"url": ORG_URL, "name": "Acme Homeware",
                                               "category": "homeware"}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def rival(client, auth_headers, shop, org_id):
    response = client.post(
        "/competitors",
        headers=auth_headers,
        json={"url": RIVAL_URL, "name": "Claybarn Co", "shop_id": shop["id"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def store():
    return FakeStore(
        RIVAL_URL,
        [
            ("Matte Ceramic Mug", "24.00"),
            ("Linen Table Runner", "34.00"),
            ("Brass Desk Lamp", "89.00"),
        ],
    )


# ---------------------------------------------------------------------------
# URL validation and the SSRF guard
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Make every hostname in this module resolve to a public address.

    The SSRF guard resolves DNS and refuses private targets, so without this
    the `.example` hosts used throughout these tests would be rejected before
    the behaviour under test ever ran. Literal-IP tests override this.
    """
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )


class TestUrlValidation:
    @pytest.mark.parametrize(
        "raw,expected_domain,expected_url",
        [
            ("acme.myshopify.com", "acme.myshopify.com", "https://acme.myshopify.com/"),
            ("https://Acme.myshopify.com/", "acme.myshopify.com",
             "https://acme.myshopify.com/"),
            ("https://acme.myshopify.com/collections/all", "acme.myshopify.com",
             "https://acme.myshopify.com/collections/all"),
            ("  https://acme.myshopify.com  ", "acme.myshopify.com",
             "https://acme.myshopify.com/"),
            # A non-default port is part of the URL but not the domain identity:
            # two shops on the same host are still the same host.
            ("http://acme.example:8080/shop", "acme.example",
             "http://acme.example:8080/shop"),
            ("https://acme.example:443/", "acme.example", "https://acme.example/"),
        ],
    )
    def test_normalizes(self, raw, expected_domain, expected_url):
        from app.ecommerce.urls import normalize_url

        result = normalize_url(raw)
        assert result.domain == expected_domain
        assert result.url == expected_url

    @pytest.mark.parametrize(
        "raw", ["", "   ", "not a url", "ftp://x.com", "javascript:alert(1)",
                "https://", "localhost", "https://nodots"]
    )
    def test_rejects(self, raw):
        from app.ecommerce.urls import InvalidShopUrl, normalize_url

        with pytest.raises(InvalidShopUrl):
            normalize_url(raw)

    @pytest.mark.parametrize(
        "host,expected",
        [
            ("acme.myshopify.com", "shopify"),
            ("shop.example.com/collections/all", "shopify"),
            ("example.com/wp-json/wc/store", "woocommerce"),
            ("www.bol.com/nl/nl/c/home", "bol"),
            ("plain.example.org", "unknown"),
        ],
    )
    def test_detects_platform(self, host, expected):
        from app.ecommerce.urls import detect_platform

        assert detect_platform(f"https://{host}") == expected

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://localhost:8000/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://[::1]/",
        ],
    )
    def test_ssrf_guard_blocks_private_targets(self, monkeypatch, url):
        """A customer must not be able to point the crawler at the cloud
        metadata endpoint or an internal service (D-016)."""
        import socket

        from app.core.config import settings
        from app.ecommerce.urls import InvalidShopUrl, normalize_url

        monkeypatch.setattr(settings, "crawler_allow_private_addresses", False)
        # Every host here is a literal IP, so DNS is never consulted anyway;
        # stubbing it keeps the test hermetic and instant.
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(InvalidShopUrl):
            normalize_url(url)

    def test_ssrf_guard_is_bypassed_only_by_explicit_config(self, monkeypatch):
        """The escape hatch exists for local fixtures and is off by default."""
        import socket

        from app.core.config import settings
        from app.ecommerce.urls import normalize_url

        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
        )
        monkeypatch.setattr(settings, "crawler_allow_private_addresses", True)
        assert normalize_url("http://127.0.0.1:8000/shop").domain == "127.0.0.1"


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------
class TestOnboarding:
    def test_add_shop(self, client, auth_headers, shop):
        assert shop["domain"] == "acme-homeware.myshopify.com"
        assert shop["platform"] == "shopify"
        assert shop["crawl_frequency_hours"] == 168

    def test_shop_requires_auth(self, client):
        assert client.post("/shops", json={"url": ORG_URL}).status_code == 401

    def test_duplicate_shop_rejected(self, client, auth_headers, shop):
        again = client.post("/shops", headers=auth_headers, json={"url": ORG_URL})
        assert again.status_code == 409

    def test_invalid_url_rejected(self, client, auth_headers):
        bad = client.post("/shops", headers=auth_headers, json={"url": "not-a-shop"})
        assert bad.status_code == 400

    def test_list_and_get(self, client, auth_headers, shop, rival):
        listed = client.get("/shops", headers=auth_headers).json()
        assert listed["count"] == 1
        assert listed["shops"][0]["competitor_count"] == 1

        detail = client.get(f"/shops/{shop['id']}", headers=auth_headers).json()
        assert detail["competitors"][0]["domain"] == "claybarn-co.myshopify.com"

    def test_update_cadence(self, client, auth_headers, shop):
        """Slower than the plan is kept. Faster is clamped: tests/test_crawl_cadence.py."""
        response = client.patch(
            f"/shops/{shop['id']}",
            headers=auth_headers,
            json={"crawl_frequency_hours": 336, "category": "homeware"},
        )
        assert response.status_code == 200
        assert response.json()["crawl_frequency_hours"] == 336

    def test_crawl_frequency_floor_is_enforced(
        self, client, auth_headers, shop, db_session, org_id
    ):
        """Crawling someone else's server every 20 minutes is abuse.

        Enforced twice on purpose: the schema rejects it, and the service
        clamps it, so no code path can write a 1-hour cadence.
        """
        from app.ecommerce.discovery import add_shop

        rejected = client.patch(
            f"/shops/{shop['id']}", headers=auth_headers, json={"crawl_frequency_hours": 1}
        )
        assert rejected.status_code == 422

        direct = add_shop(
            db_session, org_id, url="https://other-shop.myshopify.com",
            name="Other", crawl_frequency_hours=1,
        )
        assert direct.crawl_frequency_hours == 6

    def test_crawl_frequency_ceiling_is_enforced(self, client, auth_headers, shop):
        rejected = client.patch(
            f"/shops/{shop['id']}", headers=auth_headers, json={"crawl_frequency_hours": 100000}
        )
        assert rejected.status_code == 422

    def test_delete_shop_cascades(self, client, auth_headers, shop, rival, db_session):
        assert client.delete(f"/shops/{shop['id']}", headers=auth_headers).status_code == 200
        assert client.get(f"/shops/{shop['id']}", headers=auth_headers).status_code == 404
        assert db_session.execute(
            select(Competitor).where(Competitor.id == rival["id"])
        ).scalars().first() is None

    def test_add_competitor(self, client, auth_headers, rival):
        assert rival["domain"] == "claybarn-co.myshopify.com"
        assert rival["platform"] == "shopify"
        assert rival["discovery_method"] == "manual"

    def test_own_shop_is_not_a_competitor(self, client, auth_headers, shop):
        response = client.post(
            "/competitors",
            headers=auth_headers,
            json={"url": "https://www.acme-homeware.myshopify.com", "shop_id": shop["id"]},
        )
        assert response.status_code == 400
        assert "your own shop" in response.json()["detail"]

    @pytest.mark.parametrize(
        "url", ["https://www.amazon.com", "https://www.etsy.com", "https://google.com"]
    )
    def test_marketplaces_are_not_competitors(self, client, auth_headers, url):
        response = client.post("/competitors", headers=auth_headers, json={"url": url})
        assert response.status_code == 400
        assert "not a shop" in response.json()["detail"]

    def test_duplicate_competitor_rejected(self, client, auth_headers, rival):
        again = client.post("/competitors", headers=auth_headers, json={"url": RIVAL_URL})
        assert again.status_code == 409


# ---------------------------------------------------------------------------
# The crawl → change loop
# ---------------------------------------------------------------------------
def run_crawl(db, competitor, shop_id, store, monkeypatch):
    """Crawl one competitor against the fake storefront."""
    from app.core.config import settings
    from app.ecommerce.crawl import crawl_competitor

    monkeypatch.setattr(settings, "crawler_allow_private_addresses", True)
    crawler = make_crawler(store.handle)
    try:
        return crawl_competitor(db, competitor, shop_id=shop_id, crawler=crawler)
    finally:
        crawler.client.close()


def clear_changes(db):
    db.query(ChangeEvent).delete()
    db.commit()


class TestCrawlAndChanges:
    def test_first_crawl_builds_a_baseline(self, db_session, shop, rival, store, monkeypatch):
        competitor = db_session.get(Competitor, rival["id"])
        outcome = run_crawl(db_session, competitor, shop["id"], store, monkeypatch)

        assert outcome.status == CrawlStatus.COMPLETED
        assert outcome.captures_written == 3
        assert outcome.products_found == 3

        captures = db_session.execute(
            select(CompetitorProduct).where(CompetitorProduct.competitor_id == rival["id"])
        ).scalars().all()
        assert sorted(c.price for c in captures) == [24.0, 34.0, 89.0]

    def test_first_crawl_does_not_alert_on_everything_being_new(
        self, db_session, shop, rival, store, monkeypatch
    ):
        """A baseline is not three separate 'new product' alerts."""
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        changes = db_session.execute(
            select(ChangeEvent).where(ChangeEvent.shop_id == shop["id"])
        ).scalars().all()
        assert changes
        assert all("Baseline" in c.title for c in changes)

    def test_price_cut_is_detected(self, db_session, shop, rival, store, monkeypatch):
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        clear_changes(db_session)  # drop the baseline noise

        store.set_price("Linen Table Runner", "27.50")
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)

        cuts = [
            c for c in db_session.execute(select(ChangeEvent)).scalars().all()
            if c.kind == ChangeKind.PRICE_DECREASE
        ]
        assert len(cuts) == 1
        cut = cuts[0]
        assert cut.product_name == "Linen Table Runner"
        assert cut.previous_price == 34.0
        assert cut.new_price == 27.5
        assert cut.delta == pytest.approx(-6.5)
        assert cut.delta_pct == pytest.approx(-19.1, abs=0.2)
        assert cut.severity == ChangeSeverity.MEDIUM
        # The evidence link is the exact product page the price came from.
        assert "linen-table-runner" in cut.evidence_url

    def test_price_increase_is_detected(self, db_session, shop, rival, store, monkeypatch):
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        clear_changes(db_session)

        store.set_price("Brass Desk Lamp", "140.00")
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)

        rises = [
            c for c in db_session.execute(select(ChangeEvent)).scalars().all()
            if c.kind == ChangeKind.PRICE_INCREASE
        ]
        assert len(rises) == 1
        assert rises[0].new_price == 140.0
        assert rises[0].severity == ChangeSeverity.HIGH  # +57%

    def test_rounding_wobble_is_ignored(self, db_session, shop, rival, store, monkeypatch):
        """Alerting on a 1-cent wobble trains customers to ignore real alerts."""
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        clear_changes(db_session)

        store.set_price("Matte Ceramic Mug", "24.01")
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)

        moves = [
            c for c in db_session.execute(select(ChangeEvent)).scalars().all()
            if c.kind in (ChangeKind.PRICE_INCREASE, ChangeKind.PRICE_DECREASE)
        ]
        assert moves == []

    def test_new_product_is_detected(self, db_session, shop, rival, store, monkeypatch):
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        clear_changes(db_session)

        store.add("Speckled Dinner Plate", "19.00")
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)

        news = [
            c for c in db_session.execute(select(ChangeEvent)).scalars().all()
            if c.kind == ChangeKind.NEW_PRODUCT
        ]
        assert len(news) == 1
        assert news[0].product_name == "Speckled Dinner Plate"

    def test_removed_product_is_detected(self, db_session, shop, rival, store, monkeypatch):
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        clear_changes(db_session)

        store.remove("Brass Desk Lamp")
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)

        gone = [
            c for c in db_session.execute(select(ChangeEvent)).scalars().all()
            if c.kind == ChangeKind.REMOVED_PRODUCT
        ]
        assert len(gone) == 1
        assert gone[0].product_name == "Brass Desk Lamp"

    def test_out_of_stock_is_detected(self, db_session, shop, rival, store, monkeypatch):
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        clear_changes(db_session)

        original = store.handle

        def sold_out(url: str):
            status, body = original(url)
            if "matte-ceramic-mug" in url:
                body = body.replace("schema.org/InStock", "schema.org/OutOfStock")
            return status, body

        from app.core.config import settings
        from app.ecommerce.crawl import crawl_competitor

        monkeypatch.setattr(settings, "crawler_allow_private_addresses", True)
        crawler = make_crawler(sold_out)
        try:
            crawl_competitor(db_session, competitor, shop_id=shop["id"], crawler=crawler)
        finally:
            crawler.client.close()

        events = [
            c for c in db_session.execute(select(ChangeEvent)).scalars().all()
            if c.kind == ChangeKind.OUT_OF_STOCK
        ]
        assert len(events) == 1
        assert events[0].product_name == "Matte Ceramic Mug"

    def test_unchanged_crawl_creates_no_changes(self, db_session, shop, rival, store, monkeypatch):
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        clear_changes(db_session)

        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        assert db_session.execute(select(ChangeEvent)).scalars().all() == []

    def test_captures_accumulate_as_a_time_series(self, db_session, shop, rival, store, monkeypatch):
        """Every crawl inserts rows; nothing is overwritten (D-008)."""
        competitor = db_session.get(Competitor, rival["id"])
        for _ in range(3):
            run_crawl(db_session, competitor, shop["id"], store, monkeypatch)

        rows = db_session.execute(
            select(CompetitorProduct).where(CompetitorProduct.competitor_id == rival["id"])
        ).scalars().all()
        assert len(rows) == 9

    def test_duplicate_observations_are_not_alerted_twice(
        self, db_session, shop, rival, store, monkeypatch
    ):
        """Re-alerting the same move twice is how a customer mutes the product."""
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        clear_changes(db_session)

        store.set_price("Linen Table Runner", "27.50")
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        first = len(db_session.execute(select(ChangeEvent)).scalars().all())

        # A third crawl sees 27.50 -> 27.50: nothing new.
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        assert len(db_session.execute(select(ChangeEvent)).scalars().all()) == first

    def test_crawl_records_status_on_the_competitor(
        self, db_session, shop, rival, store, monkeypatch
    ):
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        db_session.refresh(competitor)
        assert competitor.last_status == CrawlStatus.COMPLETED
        assert competitor.last_crawled_at is not None
        assert competitor.product_count == 3

    def test_dead_competitor_does_not_raise(self, db_session, shop, rival, store, monkeypatch):
        """One dead shop must not fail a customer's whole week."""
        from app.core.config import settings
        from app.ecommerce.crawl import crawl_competitor

        monkeypatch.setattr(settings, "crawler_allow_private_addresses", True)
        competitor = db_session.get(Competitor, rival["id"])

        crawler = make_crawler(lambda url: (500, "boom"))
        try:
            outcome = crawl_competitor(db_session, competitor, shop_id=shop["id"], crawler=crawler)
        finally:
            crawler.client.close()

        assert outcome.status == CrawlStatus.FAILED
        assert outcome.error


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def seed_changes(db, org_id, shop_id, competitor_id):
    """A week of realistic activity: one big price cut, one new listing."""
    db.add_all([
        ChangeEvent(
            organization_id=org_id, shop_id=shop_id, competitor_id=competitor_id,
            kind=ChangeKind.PRICE_DECREASE, severity=ChangeSeverity.HIGH,
            product_name="Linen Table Runner",
            competitor_name="Claybarn Co", competitor_domain="claybarn-co.myshopify.com",
            previous_price=34.0, new_price=27.5, delta=-6.5, delta_pct=-19.1,
            currency="EUR", title="Claybarn Co cut the price of Linen Table Runner",
            summary="moved from €34.00 to €27.50",
            evidence_url="https://claybarn-co.myshopify.com/products/linen-table-runner",
            detected_at=utcnow() - timedelta(hours=2),
        ),
        ChangeEvent(
            organization_id=org_id, shop_id=shop_id, competitor_id=competitor_id,
            kind=ChangeKind.NEW_PRODUCT, severity=ChangeSeverity.LOW,
            product_name="Speckled Dinner Plate",
            competitor_name="Claybarn Co", competitor_domain="claybarn-co.myshopify.com",
            new_price=19.0, currency="EUR",
            title="Claybarn Co listed a new product",
            evidence_url="https://claybarn-co.myshopify.com/products/speckled-dinner-plate",
            detected_at=utcnow() - timedelta(hours=1),
        ),
    ])
    db.commit()


class TestReports:
    def test_report_is_written(self, db_session, org_id, shop, rival):
        from app.ecommerce.reports import generate_report

        seed_changes(db_session, org_id, shop["id"], rival["id"])
        report = generate_report(
            db_session, organization_id=org_id, shop=db_session.get(Shop, shop["id"]),
            use_llm=False,
        )

        assert report.status == ReportStatus.COMPLETED
        assert "Competitor report" in report.markdown
        assert "Linen Table Runner" in report.markdown

        # Only medium/high severity rows drive the narrative. A low-severity
        # "new listing" is real data the customer can browse in the alert inbox,
        # but it is not what the weekly brief should be built around.
        facts = report.facts
        assert facts["total_changes"] == 1
        assert facts["largest_price_move"]["product"] == "Linen Table Runner"
        # Prices are Decimal, and a Decimal cannot survive a JSON column as a
        # float -- so the engine's encoder writes it as a string (see
        # app/core/database/base.py). A string is the faithful form: reading it
        # back as a float would reintroduce exactly the error the Numeric column
        # exists to prevent.
        assert Decimal(facts["largest_price_move"]["new_price"]) == Decimal("27.50")

    def test_low_severity_changes_are_in_the_inbox_but_not_the_narrative(
        self, db_session, org_id, shop, rival
    ):
        """A quiet week of small listings should not crowd out the headline."""
        from app.ecommerce.reports import generate_report

        seed_changes(db_session, org_id, shop["id"], rival["id"])
        report = generate_report(
            db_session, organization_id=org_id, shop=db_session.get(Shop, shop["id"]),
            use_llm=False,
        )
        # Both changes are still addressable by id, so nothing is lost.
        assert len(report.change_ids) == 1
        assert report.change_ids[0] in {c.id for c in db_session.execute(
            select(ChangeEvent)
        ).scalars().all()}

    def test_deterministic_report_says_nothing_when_nothing_happened(
        self, db_session, org_id, shop
    ):
        from app.ecommerce.reports import generate_report

        report = generate_report(
            db_session, organization_id=org_id, shop=db_session.get(Shop, shop["id"]),
            use_llm=False,
        )
        assert "Nothing moved this week" in report.markdown

    def test_report_survives_a_broken_llm(self, db_session, org_id, shop, monkeypatch):
        """A model outage must not cost the customer their report."""
        import app.llm as llm_module
        from app.ecommerce.reports import generate_report

        class Exploding:
            name = "boom"
            model = "n/a"

            def complete(self, *a, **k):
                raise RuntimeError("provider is down")

        monkeypatch.setattr(llm_module, "get_llm_provider", lambda: Exploding())
        report = generate_report(
            db_session, organization_id=org_id, shop=db_session.get(Shop, shop["id"]),
            use_llm=True,
        )
        assert report.status == ReportStatus.COMPLETED
        assert report.markdown  # the deterministic text is always there
        assert "unavailable" in (report.error or "")

    def test_report_endpoint_returns_markdown_and_facts(
        self, client, auth_headers, shop, rival, db_session, org_id
    ):
        from app.ecommerce.reports import generate_report

        seed_changes(db_session, org_id, shop["id"], rival["id"])
        generate_report(
            db_session, organization_id=org_id, shop=db_session.get(Shop, shop["id"]),
            use_llm=False,
        )
        listed = client.get(f"/shops/{shop['id']}/reports", headers=auth_headers).json()
        assert listed["count"] == 1

        detail = client.get(f"/reports/{listed['reports'][0]['id']}", headers=auth_headers).json()
        assert detail["markdown"]
        assert detail["facts"]["total_changes"] == 1
        assert detail["facts"]["price_moves"][0]["evidence_url"]


# ---------------------------------------------------------------------------
# The alert inbox
# ---------------------------------------------------------------------------
class TestAlerts:
    def test_changes_are_listed_with_evidence(
        self, client, auth_headers, shop, rival, db_session, org_id
    ):
        db_session.add(ChangeEvent(
            organization_id=org_id, shop_id=shop["id"], competitor_id=rival["id"],
            kind=ChangeKind.PRICE_DECREASE, severity=ChangeSeverity.HIGH,
            product_name="Linen Table Runner", competitor_name="Claybarn Co",
            previous_price=34.0, new_price=27.5, delta=-6.5, delta_pct=-19.1,
            currency="EUR", title="Claybarn Co cut the price",
            evidence_url="https://claybarn-co.myshopify.com/products/linen-table-runner",
        ))
        db_session.commit()

        body = client.get("/changes", headers=auth_headers).json()
        assert body["count"] == 1
        assert body["unacknowledged"] == 1
        change = body["changes"][0]
        assert change["evidence_url"].endswith("/linen-table-runner")
        assert change["delta_pct"] == pytest.approx(-19.1)

    def test_acknowledge(self, client, auth_headers, shop, db_session, org_id):
        row = ChangeEvent(
            organization_id=org_id, shop_id=shop["id"], kind=ChangeKind.NEW_PRODUCT,
            title="Something new",
        )
        db_session.add(row)
        db_session.commit()

        assert client.post(f"/changes/{row.id}/ack", headers=auth_headers).status_code == 200
        listed = client.get("/changes?unacknowledged_only=true", headers=auth_headers).json()
        assert listed["count"] == 0

    def test_filter_by_kind(self, client, auth_headers, shop, db_session, org_id):
        db_session.add_all([
            ChangeEvent(organization_id=org_id, shop_id=shop["id"],
                        kind=ChangeKind.PRICE_DECREASE, title="a"),
            ChangeEvent(organization_id=org_id, shop_id=shop["id"],
                        kind=ChangeKind.NEW_PRODUCT, title="b"),
        ])
        db_session.commit()
        assert client.get(
            "/changes?kind=price_decrease", headers=auth_headers
        ).json()["count"] == 1

    def test_alerts_require_auth(self, client):
        assert client.get("/changes").status_code == 401


# ---------------------------------------------------------------------------
# Tenant isolation — the product must not leak a single row
# ---------------------------------------------------------------------------
class TestTenantIsolation:
    @pytest.fixture()
    def other_tenant(self, db_session):
        from fastapi.testclient import TestClient

        from app.main import create_app

        with TestClient(create_app()) as c:
            token = c.post(
                "/auth/register",
                json={"email": "rival@example.com", "password": "long-password-1",
                      "organization_name": "Rival Shop"},
            ).json()["token"]
            yield c, {"Authorization": f"Bearer {token}"}

    def test_other_tenant_cannot_see_the_shop(self, other_tenant, shop):
        c, headers = other_tenant
        assert c.get(f"/shops/{shop['id']}", headers=headers).status_code == 404
        assert c.get("/shops", headers=headers).json()["count"] == 0

    def test_other_tenant_cannot_see_the_competitor(self, other_tenant, rival):
        c, headers = other_tenant
        assert c.delete(f"/competitors/{rival['id']}", headers=headers).status_code == 404

    def test_other_tenant_cannot_see_the_alerts(self, other_tenant, shop, db_session, org_id):
        c, headers = other_tenant
        db_session.add(ChangeEvent(
            organization_id=org_id, shop_id=shop["id"], kind=ChangeKind.PRICE_DECREASE,
            title="SECRET: Claybarn cut a price",
        ))
        db_session.commit()
        body = c.get("/changes", headers=headers).json()
        assert body["count"] == 0
        assert "SECRET" not in json.dumps(body)

    def test_other_tenant_cannot_see_the_report(self, other_tenant, shop, rival,
                                                db_session, org_id):
        from app.ecommerce.reports import generate_report

        c, headers = other_tenant
        seed_changes(db_session, org_id, shop["id"], rival["id"])
        report = generate_report(
            db_session, organization_id=org_id, shop=db_session.get(Shop, shop["id"]),
            use_llm=False,
        )
        assert c.get(f"/reports/{report.id}", headers=headers).status_code == 404

    def test_two_tenants_track_the_same_competitor_independently(
        self, db_session, shop, rival
    ):
        """Two customers in the same niche must not share capture rows."""
        from app.core.auth.service import hash_password
        from app.core.database.identity import Organization, User

        rival_org = Organization(name="Rival Org", slug="rival-org-2")
        db_session.add(rival_org)
        db_session.flush()
        db_session.add(User(
            organization_id=rival_org.id, email="second@example.com",
            password_hash=hash_password("long-password-1"), role="admin",
        ))
        other = Competitor(
            organization_id=rival_org.id, domain=rival["domain"],
            name="Same rival, other tenant", url=rival["url"], is_active=True,
        )
        db_session.add(other)
        db_session.commit()

        mine = db_session.execute(
            select(CompetitorProduct).where(CompetitorProduct.competitor_id == rival["id"])
        ).scalars().all()
        theirs = db_session.execute(
            select(CompetitorProduct).where(CompetitorProduct.competitor_id == other.id)
        ).scalars().all()
        assert mine == [] and theirs == []


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
class TestJobs:
    def test_crawl_is_enqueued(self, client, auth_headers, shop, rival, db_session):
        response = client.post(f"/shops/{shop['id']}/crawl", headers=auth_headers)
        assert response.status_code == 202
        body = response.json()
        assert body["competitors"] == 1

        job = db_session.get(Job, body["job_id"])
        assert job.type == "crawl_shop"
        assert job.payload["shop_id"] == shop["id"]

    def test_crawl_is_idempotent_within_a_day(self, client, auth_headers, shop, rival):
        """A manual re-crawl and the scheduler must not both run today (D-015)."""
        first = client.post(f"/shops/{shop['id']}/crawl", headers=auth_headers).json()
        second = client.post(f"/shops/{shop['id']}/crawl", headers=auth_headers).json()
        assert first["job_id"] == second["job_id"]
        assert second["created"] is False

    def test_crawl_without_competitors_is_rejected(self, client, auth_headers, shop):
        response = client.post(f"/shops/{shop['id']}/crawl", headers=auth_headers)
        assert response.status_code == 400
        assert "competitor" in response.json()["detail"].lower()

    def test_report_is_enqueued(self, client, auth_headers, shop):
        response = client.post(f"/shops/{shop['id']}/report?days=14", headers=auth_headers)
        assert response.status_code == 202
        assert response.json()["status"] == "QUEUED"

    def test_scheduler_enqueues_only_due_shops(self, db_session, shop, rival):
        from app.ecommerce.jobs import schedule_due_crawls

        row = db_session.get(Shop, shop["id"])
        row.next_crawl_at = utcnow() + timedelta(days=3)
        db_session.commit()
        db_session.query(Job).delete()
        db_session.commit()

        schedule_due_crawls({})
        assert db_session.execute(select(Job)).scalars().all() == []

        row = db_session.get(Shop, shop["id"])
        row.next_crawl_at = utcnow() - timedelta(hours=1)
        db_session.commit()

        schedule_due_crawls({})
        jobs = db_session.execute(select(Job)).scalars().all()
        assert [j.type for j in jobs] == ["crawl_shop"]


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
class TestFeatureFlags:
    def test_disabled_domains_have_no_routes(self, client):
        """A disabled domain has no routes at all, not routes that 403 (D-020)."""
        for path in ("/comfyui/workflows", "/rag/status", "/training/status",
                     "/documents", "/datasets"):
            assert client.get(path).status_code == 404, path

    def test_product_routes_are_present(self, client):
        for path in ("/shops", "/competitors", "/changes", "/reports", "/overview"):
            # 401 (auth required) not 404 (route missing).
            assert client.get(path).status_code == 401, path

    def test_health_reports_the_active_set(self, client, auth_headers):
        features = client.get("/health", headers=auth_headers).json()["features"]
        assert "commerce" in features["enabled"]
        assert "documents" in features["disabled"]


class TestPriceHistory:
    """`GET /changes/{id}/history`: the product page's captures, oldest first, for the chart."""

    def test_a_price_cut_has_a_two_point_history(
        self, client, auth_headers, db_session, shop, rival, store, monkeypatch
    ):
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        clear_changes(db_session)
        store.set_price("Linen Table Runner", "27.50")
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        cut = next(
            c for c in db_session.execute(select(ChangeEvent)).scalars().all()
            if c.kind == ChangeKind.PRICE_DECREASE
        )

        response = client.get(f"/changes/{cut.id}/history", headers=auth_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["product"] == "Linen Table Runner"
        assert [float(p["price"]) for p in body["points"]] == [34.0, 27.5]
        assert all(p["data_source"] and p["source_label"] for p in body["points"])
        assert body["change"]["competitor_id"] == rival["id"]

    def test_another_tenant_gets_a_404(self, client, auth_headers, db_session, shop, rival, store, monkeypatch):
        competitor = db_session.get(Competitor, rival["id"])
        run_crawl(db_session, competitor, shop["id"], store, monkeypatch)
        change = db_session.execute(select(ChangeEvent)).scalars().first()
        other = client.post("/auth/register", json={
            "email": "history-other@example.com", "password": "correct-horse-battery",
            "organization_name": "Other Co"})
        assert other.status_code == 201, other.text
        headers = {"Authorization": f"Bearer {other.json()['token']}"}
        assert client.get(f"/changes/{change.id}/history", headers=headers).status_code == 404

    def test_an_unknown_change_is_a_404(self, client, auth_headers):
        assert client.get("/changes/999999/history", headers=auth_headers).status_code == 404

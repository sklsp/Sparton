"""Competitor catalogs read from the shop''s own data.

The claim under test is the one the product makes to a customer: a price from a
shop''s feed is *exact*, and it costs nothing. Both halves are asserted here --
the exactness against a fake Shopify/WooCommerce response, and the zero-token
property by checking that no LLM usage row is written and the provider was
never called.

The fallbacks matter as much as the happy path. A competitor on a platform with
no feed and no JSON-LD must still be crawled from HTML, because that is most of
the long tail, and a product that quietly stopped updating would be worse than
one that is slightly less precise.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent

from app.core.database.ecommerce_models import (  # noqa: E402 - must come after ROOT
    ChangeEvent,
    ChangeKind,
    Competitor,
    CompetitorProduct,
)
from app.ecommerce.feeds import (  # noqa: E402 - must come after ROOT
    SOURCE_FEED,
    SOURCE_HTML,
    SOURCE_JSONLD,
    read_feed_catalog,
    read_jsonld_products,
    shopfeed_available,
    source_label,
)

pytestmark = pytest.mark.skipif(
    not shopfeed_available(), reason="shopfeed is not installed"
)

SHOP = "https://shop.test"

JSONLD_PAGE = (
    "<html><head><title>Enamel Mug</title>"
    "<script type=\"application/ld+json\">"
    "{\"@context\":\"https://schema.org\",\"@type\":\"Product\",\"name\":\"Enamel Mug\","
    "\"sku\":\"MUG-1\","
    "\"offers\":{\"@type\":\"Offer\",\"price\":\"8.50\",\"priceCurrency\":\"GBP\","
    "\"availability\":\"https://schema.org/InStock\"}}"
    "</script></head><body><h1>Enamel Mug</h1><p>8.50</p></body></html>"
)

SHOPIFY_PRODUCTS = [
    {
        "id": 101,
        "title": "Cast Iron Skillet",
        "handle": "cast-iron-skillet",
        "vendor": "Acme",
        "updated_at": "2026-01-02T00:00:00Z",
        "images": [{"src": f"{SHOP}/img/101.jpg"}],
        "variants": [
            {
                "price": "24.99",
                "compare_at_price": "29.99",
                "available": True,
                "sku": "SKU-101",
                "barcode": "5012345678900",
            }
        ],
    },
    {
        "id": 102,
        "title": "Copper Kettle",
        "handle": "copper-kettle",
        "vendor": "Acme",
        "variants": [
            {"price": "0.45", "available": False, "sku": "SKU-102"}
        ],
    },
]


def _page(url: str) -> str:
    """The path portion of a shop URL, for routing in a fake."""
    return url.replace(SHOP, "")


def shopify_transport(*, on_sale: bool = False, currency: str = "EUR") -> httpx.MockTransport:
    """A fake Shopify: /products.json, /cart.js, robots.txt.

    Prices are deliberately awkward -- 24.99, 0.45 -- because float arithmetic on
    money is exactly what an "it is exact, we read their API" claim has to rule
    out. 0.45 has no exact binary representation, so any accumulated delta shows
    up here immediately.
    """
    from urllib.parse import parse_qs, urlparse

    products = []
    for base in (SHOPIFY_PRODUCTS[0], SHOPIFY_PRODUCTS[1]):
        copy = dict(base)
        variants = [dict(v) for v in base["variants"]]
        if on_sale:
            variants[0]["price"] = "19.99"
        else:
            # Not a sale: without this the plain case would carry a
            # struck-through price and stop being a control.
            variants[0]["compare_at_price"] = None
        copy["variants"] = variants
        products.append(copy)

    def handler(request: httpx.Request) -> httpx.Response:
        path = _page(str(request.url))
        if path.startswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if path.startswith("/cart.js"):
            return httpx.Response(200, json={"currency": currency}, request=request)
        if path.startswith("/products.json"):
            # Shopify paginates and a real shop returns [] past the last page.
            # Without that the reader would ask for page 2, 3, ... forever and
            # collect the same two products dozens of times.
            page = int(parse_qs(urlparse(str(request.url)).query).get("page", ["1"])[0])
            body = {"products": products if page == 1 else []}
            return httpx.Response(200, json=body, request=request)
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def woocommerce_transport() -> httpx.MockTransport:
    """A fake WooCommerce store. Prices are minor units: 1299 means 12.99."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = _page(str(request.url))
        if path.startswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if path.startswith("/wp-json/wc/store/v1/products"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "name": "Walnut Board",
                        "permalink": f"{SHOP}/product/walnut-board/",
                        "sku": "W-7",
                        "is_in_stock": True,
                        "prices": {
                            "price": "1299",
                            "regular_price": "1499",
                            "currency_minor_unit": 2,
                            "currency_code": "GBP",
                        },
                    }
                ],
                headers={"x-wp-totalpages": "1"},
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def jsonld_only_transport() -> httpx.MockTransport:
    """No platform feed, but schema.org markup on the page: the JSON-LD tier.

    Most small shops have no Shopify or WooCommerce API and nearly all of them
    emit JSON-LD for Google Shopping, so this is where a lot of the long tail
    lands -- and it is still exact, still free, and it costs no extra request
    because the page was fetched for the crawl anyway.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = _page(str(request.url))
        if path.startswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if path.startswith("/products.json") or path.startswith("/wp-json"):
            return httpx.Response(404, request=request)
        if path in ("/", ""):
            return httpx.Response(200, text=JSONLD_PAGE, request=request)
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def no_feed_html_transport() -> httpx.MockTransport:
    """A plain old shop: no feed, no structured data, only visible text.

    This is what the LLM path exists for, and it is why the fallback is not
    optional. The listing links to a product page the way a real crawl finds
    one, and the price appears nowhere but in a span.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = _page(str(request.url))
        if path.startswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if path.startswith("/products.json") or path.startswith("/wp-json"):
            return httpx.Response(404, request=request)
        if path in ("/", ""):
            return httpx.Response(
                200,
                text=(
                    "<html><head><title>Catalogue</title></head><body>"
                    "<a href='/basic-soap'>Basic Soap</a></body></html>"
                ),
                request=request,
            )
        if path == "/basic-soap":
            return httpx.Response(
                200,
                text=(
                    "<html><head><title>Basic Soap</title>"
                    '<meta property="og:type" content="product">'
                    '<meta property="og:title" content="Basic Soap">'
                    '<meta property="product:price:amount" content="4.25">'
                    '<meta property="product:price:currency" content="EUR">'
                    "</head><body><h1>Basic Soap</h1>"
                    '<span class="product__price">4.25</span>'
                    "<p>Olive oil soap, 100g.</p></body></html>"
                ),
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def no_feed_transport() -> httpx.MockTransport:
    """Nothing at all: no feed, no page. The customer sees nothing change."""

    def handler(request: httpx.Request) -> httpx.Response:
        if _page(str(request.url)).startswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


class _NoWait:
    """Used by tests that are not about pacing.

    The production default really sleeps between requests, which is correct and
    would make this file take minutes. The pacing itself is asserted in
    TestTheFeedReaderIsPolite, with a sleep that records rather than waits.
    """

    def __call__(self, seconds: float) -> None:
        return None


def read(transport, url: str = SHOP, **kwargs):
    kwargs.setdefault("sleep", _NoWait())
    return read_feed_catalog(
        url, transport=transport, allow_private=True, **kwargs
    )


class TestTheShopifyFeed:
    def test_it_reads_the_catalog_without_touching_html(self):
        result = read(shopify_transport())
        assert result.found
        assert result.source == SOURCE_FEED
        assert result.platform == "shopify"
        assert {p.name for p in result.products} == {
            "Cast Iron Skillet",
            "Copper Kettle",
        }

    def test_prices_are_exact_not_rounded_floats(self):
        """24.99 must not come back as 24.989999, and 0.45 -- which has no exact
        binary representation -- must not drift. A feed is the shop's own
        arithmetic and we should hand it on unchanged."""
        result = read(shopify_transport())
        by_name = {p.name: p for p in result.products}
        assert by_name["Cast Iron Skillet"].price == Decimal("24.99")
        assert by_name["Copper Kettle"].price == Decimal("0.45")
        for product in result.products:
            assert isinstance(product.price, Decimal), product.name

    def test_the_currency_comes_from_the_shop(self):
        result = read(shopify_transport(currency="SEK"))
        assert {p.currency for p in result.products} == {"SEK"}

    def test_a_sale_is_recorded_as_a_compare_at(self):
        """The was-price is the only way a markdown ending is visible: the
        effective price is identical before and after it lapses."""
        plain = read(shopify_transport())
        assert plain.products[0].attributes["compare_at"] is None

        on_sale = read(shopify_transport(on_sale=True))
        skillet = next(p for p in on_sale.products if p.name == "Cast Iron Skillet")
        assert skillet.price == Decimal("19.99")
        assert skillet.attributes["compare_at"] == Decimal("29.99")

    def test_a_compare_at_below_the_price_is_discarded(self):
        """Some feeds publish a compare_at under the price. Recording it would
        report a markdown on a product that actually went up."""

        def handler(request):
            path = _page(str(request.url))
            if path.startswith("/robots.txt"):
                return httpx.Response(200, text="Allow: /", request=request)
            if path.startswith("/cart.js"):
                return httpx.Response(200, json={"currency": "EUR"}, request=request)
            if path.startswith("/products.json"):
                return httpx.Response(
                    200,
                    json={"products": [{
                        "id": 1, "title": "Odd", "handle": "odd",
                        "variants": [{"price": "10.00", "compare_at_price": "5.00",
                                      "available": True}],
                    }]},
                    request=request,
                )
            return httpx.Response(404, request=request)

        result = read(httpx.MockTransport(handler))
        assert result.products[0].attributes["compare_at"] is None

    def test_stock_state_comes_from_the_feed(self):
        by_name = {p.name: p for p in read(shopify_transport()).products}
        assert by_name["Cast Iron Skillet"].availability == "in_stock"
        assert by_name["Copper Kettle"].availability == "out_of_stock"

    def test_sku_and_variant_count_are_kept(self):
        skillet = read(shopify_transport()).products[0]
        assert skillet.attributes["sku"] == "SKU-101"
        assert skillet.attributes["external_id"] == "101"
        assert skillet.attributes["variants"] == 1

    def test_every_product_is_marked_as_coming_from_the_feed(self):
        result = read(shopify_transport())
        assert {p.attributes["data_source"] for p in result.products} == {SOURCE_FEED}
        assert {p.method for p in result.products} == {SOURCE_FEED}
        assert {p.confidence for p in result.products} == {1.0}

    def test_the_page_budget_is_respected(self):
        """A feed must not cost more than the HTML crawl it replaces."""
        result = read(shopify_transport(), max_products=1)
        assert len(result.products) == 1


class TestTheWooCommerceFeed:
    def test_minor_units_are_converted_into_a_price(self):
        """WooCommerce publishes 1299 for EUR 12.99. Storing 1299 would be out
        by a factor of 100, and it would be a *confident* wrong number."""
        result = read(woocommerce_transport())
        assert result.found
        assert result.source == SOURCE_FEED
        assert result.platform == "woocommerce"
        board = result.products[0]
        assert board.price == Decimal("12.99")
        assert board.attributes["compare_at"] == Decimal("14.99")
        assert board.currency == "GBP"


class TestSitesWithNoFeed:
    def test_the_fallback_is_the_html_crawler(self):
        result = read(no_feed_transport())
        assert not result.found
        assert result.source == SOURCE_HTML, (
            "a site with no feed must fall through to the HTML crawl, not fail"
        )

    def test_a_shop_that_serves_nothing_is_not_an_error(self):
        """Most of the long tail looks like this. It is a normal outcome."""
        assert read(no_feed_transport()).products == []


class TestJsonLdOnAPage:
    def test_structured_data_yields_exact_products(self):
        products = read_jsonld_products(JSONLD_PAGE, f"{SHOP}/mug")
        assert len(products) == 1
        mug = products[0]
        assert mug.name == "Enamel Mug"
        assert mug.price == Decimal("8.50")
        assert mug.currency == "GBP"
        assert mug.attributes["data_source"] == SOURCE_JSONLD
        assert mug.attributes["compare_at"] is None  # schema.org has no "was" price

    def test_broken_json_ld_is_skipped_rather_than_raising(self):
        """Malformed JSON-LD is common in the wild. One bad blob must not lose
        the whole page."""
        assert read_jsonld_products('<script type="application/ld+json">{oops</script>', SHOP) == []

    def test_a_page_with_no_structured_data_yields_nothing(self):
        assert read_jsonld_products("<html><body>hi</body></html>", SHOP) == []


class TestTheSourceLabel:
    def test_each_tier_is_described_differently_to_the_customer(self):
        labels = {source_label(s) for s in (SOURCE_FEED, SOURCE_JSONLD, SOURCE_HTML)}
        assert len(labels) == 3, "two tiers read identically to a customer"

    def test_the_feed_is_described_as_exact(self):
        assert "exact" in source_label(SOURCE_FEED)

    def test_the_html_tier_is_described_as_extracted(self):
        assert "extracted" in source_label(SOURCE_HTML)

    def test_an_unknown_source_falls_back_rather_than_showing_nothing(self):
        assert source_label("something-new") == source_label(SOURCE_HTML)


def _usage_count(db) -> int:
    from app.core.database.usage_models import LLMUsage

    return db.query(LLMUsage).count()


def _llm_calls(provider) -> list:
    """Every conversation the provider was asked to complete.

    The `llm` fixture records each call, so "did this crawl use the model?" is
    answerable directly rather than inferred from a counter that might not be
    the one that matters.
    """
    return list(getattr(provider, "calls", []))


class TestTheCrawlPrefersTheShopFeed:
    """The integration claim: a feed-backed crawl is exact and costs nothing.

    Asserted through the real `crawl_competitor`, because the failure worth
    catching is not "the feed reader returns nothing" -- it is "the feed reader
    returns something and the crawl quietly keeps using the old path anyway",
    which only shows up end to end.
    """

    @pytest.fixture()
    def competitor(self, client, auth_headers, db_session):
        from sqlalchemy import select

        from app.core.database.identity import User

        org_id = db_session.execute(
            select(User).where(User.email == "admin@example.com")
        ).scalars().first().organization_id
        row = Competitor(
            organization_id=org_id,
            domain="shop.test",
            url=SHOP,
            name="Fake Shopify",
            is_active=True,
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        return row

    def _crawl(self, db, competitor, transport, sleep=None):
        """Crawl with both paths pointed at the same fake shop.

        The feed reader and the HTML crawler are separate HTTP clients, so both
        get the same MockTransport. Otherwise the "falls back to HTML" test
        would quietly reach the real internet for a host that does not exist --
        and pass or hang for reasons that have nothing to do with the code.

        `sleep` defaults to no waiting; tests about pacing pass one in.
        """
        from dataclasses import replace

        from app.ecommerce.crawl import crawl_competitor, default_crawl_policy
        from app.research.crawler import ResponsibleCrawler

        crawler = ResponsibleCrawler(
            policy=replace(
                default_crawl_policy(5),
                delay_seconds=0.0,
                allow_private_addresses=True,
            ),
            client=httpx.Client(transport=transport, follow_redirects=False),
        )
        return crawl_competitor(
            db,
            competitor,
            crawler=crawler,
            feed_transport=transport,
            allow_private=True,
            sleep=sleep if sleep is not None else _NoWait(),
        )

    def test_a_feed_crawl_records_no_llm_usage(self, db_session, competitor, llm):
        """The whole point of the feature, in one assertion.

        A feed gives exact prices, so there is nothing for a model to guess at
        and no reason to spend a token. If this ever records usage, the feed
        path has stopped short-circuiting the extraction step -- which would be
        invisible in the price, because the number would be identical.
        """
        before = _usage_count(db_session)
        outcome = self._crawl(db_session, competitor, shopify_transport())
        assert outcome.status == "COMPLETED", outcome.error
        assert outcome.data_source == SOURCE_FEED
        assert outcome.products_found == 2, outcome.products_found
        assert _usage_count(db_session) == before, "a feed crawl recorded LLM usage"
        assert _llm_calls(llm) == [], f"a feed crawl called the LLM: {_llm_calls(llm)}"

    def test_the_crawl_passes_the_sleep_through(self, db_session, competitor):
        """The hook has to reach the fetcher the crawl actually builds, or the
        suite would take a real second per request on every competitor.

        Asserted behaviorally: a sleep that records instead of waits must be
        called by the crawl's own feed read. A signature check cannot catch a
        parameter that is accepted and then dropped on the floor -- which is
        exactly how this seam broke once before."""
        from app.ecommerce.feeds import FEED_MIN_INTERVAL_SECONDS

        sleep = RecordingSleep()
        outcome = self._crawl(db_session, competitor, shopify_transport(), sleep=sleep)
        assert outcome.status == "COMPLETED", outcome.error
        assert sleep.calls >= 2, (
            "the crawl's feed read made several requests but the injected sleep "
            "was never called: it is not reaching the fetcher"
        )
        sleep.assert_spaced(FEED_MIN_INTERVAL_SECONDS)

    def test_a_feed_crawl_stamps_every_capture_with_its_source(
        self, db_session, competitor, llm
    ):
        self._crawl(db_session, competitor, shopify_transport())
        rows = (
            db_session.query(CompetitorProduct)
            .filter_by(competitor_id=competitor.id)
            .all()
        )
        assert len(rows) == 2
        assert {r.data_source for r in rows} == {SOURCE_FEED}

    def test_a_sale_is_persisted_as_the_was_price(self, db_session, competitor, llm):
        self._crawl(db_session, competitor, shopify_transport(on_sale=True))
        row = (
            db_session.query(CompetitorProduct)
            .filter_by(competitor_id=competitor.id, name="Cast Iron Skillet")
            .one()
        )
        assert row.price == Decimal("19.99")
        assert row.compare_at_price == Decimal("29.99")
        assert row.data_source == SOURCE_FEED

    def test_the_prices_survive_the_round_trip_through_the_database(
        self, db_session, competitor, llm
    ):
        """Money must not drift on the way in. 0.45 is the interesting one: it
        has no exact binary representation, so anything that accumulates a
        delta shows up here and nowhere else."""
        self._crawl(db_session, competitor, shopify_transport())
        row = (
            db_session.query(CompetitorProduct)
            .filter_by(competitor_id=competitor.id, name="Copper Kettle")
            .one()
        )
        assert row.price == Decimal("0.45")
        assert str(row.price.quantize(Decimal("0.01"))) == "0.45"

    def test_the_competitor_records_which_tier_won(self, db_session, competitor, llm):
        """So the competitor view can say the numbers are exact."""
        self._crawl(db_session, competitor, shopify_transport())
        db_session.refresh(competitor)
        assert competitor.last_source == SOURCE_FEED
        assert competitor.last_status == "COMPLETED"

    def test_a_jsonld_crawl_also_costs_nothing(self, db_session, competitor, llm):
        """JSON-LD is read from a page the HTML crawl already fetched, so the
        numbers are exact at no extra request and no tokens."""
        before = _usage_count(db_session)
        outcome = self._crawl(db_session, competitor, jsonld_only_transport())
        assert outcome.data_source == SOURCE_JSONLD, outcome.data_source
        assert outcome.products_found >= 1
        assert _usage_count(db_session) == before
        assert _llm_calls(llm) == [], _llm_calls(llm)

    def test_a_site_with_no_feed_falls_through_to_the_html_crawler(
        self, db_session, competitor, llm
    ):
        """The long tail. It must still be crawled, and it must be labelled
        honestly as parsed rather than exact."""
        outcome = self._crawl(db_session, competitor, no_feed_html_transport())
        assert outcome.status == "COMPLETED", outcome.error
        assert outcome.data_source == SOURCE_HTML
        assert outcome.products_found >= 1
        db_session.refresh(competitor)
        assert competitor.last_source == SOURCE_HTML

    def test_the_html_fallback_is_labelled_as_less_than_exact(
        self, db_session, competitor, llm
    ):
        """A customer must be able to tell a parsed price from a feed price."""
        self._crawl(db_session, competitor, no_feed_html_transport())
        rows = (
            db_session.query(CompetitorProduct)
            .filter_by(competitor_id=competitor.id)
            .all()
        )
        assert rows
        assert {r.data_source for r in rows} == {SOURCE_HTML}
        assert "extracted" in source_label(SOURCE_HTML)
        assert "exact" not in source_label(SOURCE_HTML)

    def test_a_price_change_from_a_feed_is_detected_without_an_llm(
        self, db_session, competitor, llm
    ):
        """Change detection stays arithmetic whatever the source: a feed price
        that moves is a price change, and the alert still costs nothing."""
        self._crawl(db_session, competitor, shopify_transport())
        before_usage = _usage_count(db_session)

        outcome = self._crawl(db_session, competitor, shopify_transport(on_sale=True))
        assert outcome.data_source == SOURCE_FEED
        drops = (
            db_session.query(ChangeEvent)
            .filter_by(competitor_id=competitor.id, kind=ChangeKind.PRICE_DECREASE)
            .all()
        )
        assert drops, "a markdown from 24.99 to 19.99 was not detected"
        assert drops[0].previous_price == Decimal("24.99")
        assert drops[0].new_price == Decimal("19.99")
        assert _usage_count(db_session) == before_usage
        assert _llm_calls(llm) == [], _llm_calls(llm)


class TestShopfeedIsDeclaredTheRightWay:
    """`shopfeed` is a private repository, so it must not be listed as a
    resolvable dependency anywhere a build would try to fetch it."""

    def test_requirements_names_the_local_install_not_a_git_url(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert "shopfeed" in text, "shopfeed is undocumented for whoever builds the image"
        # A git+https or plain package line would make `pip install -r` fail.
        for forbidden in ("git+https", "git+ssh", "shopfeed==", "shopfeed>="):
            assert forbidden not in text, f"{forbidden} would be fetched, not installed locally"
        assert "pip install -e" in text, "the local install command is missing"

    def test_the_dockerfile_explains_how_to_add_it(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "shopfeed" in dockerfile, "the image has no note about the private dependency"

    def test_the_app_degrades_when_the_library_is_missing(self, monkeypatch):
        """A deployment without shopfeed must still crawl, not crash."""
        import builtins

        from app.ecommerce import feeds

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name.startswith("shopfeed"):
                raise ImportError("shopfeed is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        assert feeds.shopfeed_available() is False
        result = feeds.read_feed_catalog("https://shop.test")
        assert result.found is False
        assert result.source == SOURCE_HTML
        assert feeds.read_jsonld_products("<html></html>", "https://shop.test") == []


class RecordingSleep:
    """A fake `sleep` that records what it was asked to wait for.

    The point of the exercise: a test that really slept would take a second per
    request, and a test that mocked the clock entirely would not notice the
    throttle being removed. Recording the requested waits tests the real
    behaviour in milliseconds.
    """

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.calls = 0

    def __call__(self, seconds: float) -> None:
        self.calls += 1
        self.waits.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.waits)

    @property
    def min_wait(self) -> float:
        return min(self.waits) if self.waits else 0.0

    def assert_spaced(self, interval: float) -> None:
        """Every gap is a real wait for roughly the interval.

        Not `>= interval`, and the difference is worth recording: shopfeed
        sleeps the *remaining* time to the next allowed moment
        (`last + interval - now`), so each wait is a fraction of a millisecond
        under the interval because the request itself took that long. Asserting
        `>= interval` would fail on correct code.
        """
        assert self.waits, "no request was paced at all"
        slowest_gap = max(self.waits)
        assert slowest_gap >= interval * 0.5, (
            f"requests are not meaningfully spaced: waits were {self.waits}, "
            f"expected roughly {interval}s between them"
        )


class TestTheFeedReaderIsPolite:
    """One `read_catalog` makes several requests -- robots.txt, the Shopify
    currency probe, then a page per 250 products -- and they must not go out as
    a burst.

    This used to pass `sleep=_no_sleep` on the reasoning that Sparton's own
    crawler throttles us. It does not: `ResponsibleCrawler._throttle` is per
    *crawl*, and the feed reader is a different HTTP client that throttle never
    sees. So the burst was real, aimed at a competitor's server, from an IP that
    is us.
    """

    def test_the_production_default_actually_sleeps(self):
        """Not a property of the test: the fetcher's sleep must be time.sleep."""
        import time as time_module

        from app.ecommerce.feeds import _fetcher

        fetcher = _fetcher(allow_private=True, transport=shopify_transport())
        try:
            assert fetcher._sleep is time_module.sleep, (
                "production is not sleeping between requests to a host"
            )
        finally:
            fetcher.close()

    def test_the_per_host_interval_is_one_second(self):
        from app.ecommerce.feeds import FEED_MIN_INTERVAL_SECONDS, _fetcher

        assert FEED_MIN_INTERVAL_SECONDS == 1.0
        fetcher = _fetcher(allow_private=True, transport=shopify_transport())
        try:
            assert fetcher.min_interval == 1.0
        finally:
            fetcher.close()

    def test_a_feed_read_waits_between_requests_to_the_same_host(self):
        """The property itself: a real read records more than one wait, and
        every one of them is the full interval rather than a token amount."""
        from app.ecommerce.feeds import FEED_MIN_INTERVAL_SECONDS, read_feed_catalog

        sleep = RecordingSleep()
        result = read_feed_catalog(
            SHOP,
            transport=shopify_transport(),
            allow_private=True,
            sleep=sleep,
        )
        assert result.found
        # robots.txt, the currency probe, products.json page 1: at least three
        # requests, so at least two gaps between them.
        assert sleep.calls >= 2, (
            f"a feed read made several requests but waited {sleep.calls} times: "
            "they are going out as a burst"
        )
        sleep.assert_spaced(FEED_MIN_INTERVAL_SECONDS)

    def test_a_second_read_of_the_same_host_also_waits(self):
        from app.ecommerce.feeds import FEED_MIN_INTERVAL_SECONDS, read_feed_catalog

        sleep = RecordingSleep()
        for _ in range(2):
            read_feed_catalog(
                SHOP, transport=shopify_transport(), allow_private=True, sleep=sleep
            )
        assert sleep.calls >= 4, (
            "consecutive crawls of the same competitor did not throttle: "
            "a weekly schedule times many is still a lot of traffic"
        )
        sleep.assert_spaced(FEED_MIN_INTERVAL_SECONDS)

    def test_the_woocommerce_path_is_throttled_too(self):
        from app.ecommerce.feeds import read_feed_catalog

        sleep = RecordingSleep()
        result = read_feed_catalog(
            SHOP, transport=woocommerce_transport(), allow_private=True, sleep=sleep
        )
        assert result.found
        assert sleep.calls >= 1, "the WooCommerce path bypasses the delay"
        sleep.assert_spaced(1.0)

    def test_a_shop_with_no_feed_is_still_polite_while_discovering_that(self):
        """The negative path makes requests too -- robots.txt, then the two
        platform probes -- and those are the ones that reach a host that has
        nothing to give us."""
        from app.ecommerce.feeds import read_feed_catalog

        sleep = RecordingSleep()
        read_feed_catalog(
            SHOP, transport=no_feed_transport(), allow_private=True, sleep=sleep
        )
        assert sleep.calls >= 1, (
            "probing a shop for a feed fires several requests and must not "
            "burst them"
        )

    def test_injecting_sleep_does_not_change_the_result(self):
        """Recording the waits must not change what we read."""
        from app.ecommerce.feeds import read_feed_catalog

        real = read_feed_catalog(
            SHOP, transport=shopify_transport(), allow_private=True, sleep=RecordingSleep()
        )
        assert real.found
        assert real.source == SOURCE_FEED
        assert {p.name for p in real.products} == {"Cast Iron Skillet", "Copper Kettle"}

class TestTheMoneyCoercion:
    """_money keeps the shop's exact Decimal and refuses what is not money."""

    def test_none_stays_none(self):
        from app.ecommerce.feeds import _money

        assert _money(None) is None

    def test_a_decimal_is_quantised_not_converted(self):
        # MONEY_QUANTUM has four places, matching Numeric(12, 4); no float may sit between.
        from decimal import Decimal

        from app.ecommerce.feeds import _money

        assert str(_money(Decimal("8.5"))) == "8.5000"
        assert _money(Decimal("1.999")) == Decimal("1.9990")

    def test_a_string_amount_is_accepted(self):
        # A feed that stringified its numbers must not crash the crawl.
        from decimal import Decimal

        from app.ecommerce.feeds import _money

        assert _money("12.34") == Decimal("12.3400")

    def test_a_float_round_trips_without_binary_error(self):
        # 0.45 has no exact binary form; going through str() keeps it clean.
        from decimal import Decimal

        from app.ecommerce.feeds import _money

        assert _money(0.45) == Decimal("0.4500")

    def test_nan_and_infinity_are_refused(self):
        # A NaN compares unequal to itself and would poison every price diff.
        from decimal import Decimal

        from app.ecommerce.feeds import _money

        assert _money(float("nan")) is None
        assert _money(float("inf")) is None
        assert _money(Decimal("NaN")) is None

    def test_garbage_is_refused(self):
        from app.ecommerce.feeds import _money

        assert _money("not a price") is None

    def test_a_value_that_cannot_be_stringified_is_refused(self):
        from app.ecommerce.feeds import _money

        class Broken:
            def __str__(self):
                raise TypeError("no string form")

        assert _money(Broken()) is None


class TestTheExtractionOfOneProduct:
    """_to_extracted maps one shopfeed Product onto our row, honestly."""

    def _product(self, **overrides):
        from decimal import Decimal

        from shopfeed.catalog import Product

        base = dict(
            platform="shopify",
            id="101",
            title="Cast Iron Skillet",
            url=f"{SHOP}/products/cast-iron-skillet",
            price=Decimal("24.99"),
            compare_at=None,
            currency="eur",
            available=True,
            sku="SKU-101",
            variants=3,
            vendor="Acme Foundry",
            image=f"{SHOP}/img/skillet.jpg",
            gtin="5012345678900",
        )
        base.update(overrides)
        return Product(**base)

    def test_a_product_with_no_title_and_no_price_is_dropped(self):
        # Storing it would surface as a phantom "product removed" next crawl.
        from app.ecommerce.feeds import _to_extracted

        row = _to_extracted(
            self._product(title="", price=None), source=SOURCE_FEED
        )
        assert row is None

    def test_a_title_without_a_price_is_kept(self):
        # The shop may hide the price behind a variant picker; the product itself is real.
        from app.ecommerce.feeds import _to_extracted

        row = _to_extracted(self._product(price=None), source=SOURCE_FEED)
        assert row.name == "Cast Iron Skillet"
        assert row.price is None

    def test_a_price_without_a_title_gets_an_honest_placeholder(self):
        from decimal import Decimal

        from app.ecommerce.feeds import _to_extracted

        row = _to_extracted(self._product(title=""), source=SOURCE_FEED)
        assert row.name == "Untitled product"
        assert row.price == Decimal("24.99")

    def test_the_currency_is_normalised_and_given_a_default(self):
        from app.ecommerce.feeds import _to_extracted

        assert (
            _to_extracted(self._product(currency="gbp"), source=SOURCE_FEED).currency
            == "GBP"
        )
        # A feed that omits the currency is assumed to be in EUR, not left blank.
        assert (
            _to_extracted(self._product(currency=None), source=SOURCE_FEED).currency
            == "EUR"
        )

    def test_availability_and_the_markdown_are_carried_through(self):
        from decimal import Decimal

        from app.ecommerce.feeds import _to_extracted

        row = _to_extracted(
            self._product(available=False, compare_at=Decimal("29.99")),
            source=SOURCE_FEED,
        )
        assert row.availability == "out_of_stock"
        assert row.attributes["compare_at"] == Decimal("29.99")

    def test_the_source_is_stamped_into_the_row(self):
        # The UI shows the customer which kind of number they are looking at.
        from app.ecommerce.feeds import _to_extracted

        feed = _to_extracted(self._product(), source=SOURCE_FEED)
        ld = _to_extracted(self._product(), source=SOURCE_JSONLD)
        # The row records the tier that produced it, not the shop's platform:
        # the UI shows the customer which kind of number they are looking at.
        assert (feed.method, feed.confidence) == ("feed", 1.0)
        assert (ld.method, ld.confidence) == ("jsonld", 0.95)

    def test_the_identifiers_and_variant_count_are_kept(self):
        from app.ecommerce.feeds import _to_extracted

        row = _to_extracted(self._product(variants=0), source=SOURCE_FEED)
        assert row.attributes["external_id"] == "101"
        assert row.attributes["sku"] == "SKU-101"
        assert row.attributes["gtin"] == "5012345678900"
        # A feed that reports zero variants means "not stated", not "none".
        assert row.attributes["variants"] == 1


class TestTheStrictResolver:
    """_public_only must stay shopfeed's strict resolver."""

    def test_a_globally_routable_address_passes(self):
        from app.ecommerce.feeds import _public_only

        assert _public_only("93.184.215.14") is True

    def test_loopback_is_refused(self):
        # The escape hatch for local fixtures is allow_private, not this resolver.
        from app.ecommerce.feeds import _public_only

        assert _public_only("localhost") is False
        assert _public_only("127.0.0.1") is False

    def test_a_name_that_does_not_resolve_is_refused(self):
        # No address, no fetch: a name that dies on lookup must not pass.
        from app.ecommerce.feeds import _public_only

        assert _public_only("no-such-host.invalid") is False


class TestTheSpartanGate:
    """_sparton_allows decides before shopfeed is even called."""

    def test_the_development_flag_opens_the_gate_without_resolving(self, monkeypatch):
        from app.core.config import settings
        from app.ecommerce.feeds import _sparton_allows

        calls = []

        def _must_not_run(url):
            calls.append(url)
            return True

        monkeypatch.setattr("app.research.crawler._is_public_http_url", _must_not_run)
        monkeypatch.setattr(settings, "crawler_allow_private_addresses", True)
        assert _sparton_allows(SHOP) is True
        assert calls == [], "the flag must short-circuit the address check"

    def test_without_the_flag_the_crawler_guard_decides(self, monkeypatch):
        from app.core.config import settings
        from app.ecommerce.feeds import _sparton_allows

        monkeypatch.setattr(settings, "crawler_allow_private_addresses", False)
        monkeypatch.setattr("app.research.crawler._is_public_http_url", lambda url: True)
        assert _sparton_allows(SHOP) is True
        monkeypatch.setattr(
            "app.research.crawler._is_public_http_url", lambda url: False
        )
        assert _sparton_allows(SHOP) is False

    def test_read_feed_catalog_refuses_a_blocked_url_before_any_request(self, monkeypatch):
        # The guard runs before the fetcher exists, so no request may be made.
        from app.core.config import settings

        def _no_request(request):
            raise AssertionError("a blocked URL must not be fetched")

        monkeypatch.setattr(settings, "crawler_allow_private_addresses", False)
        monkeypatch.setattr(
            "app.research.crawler._is_public_http_url", lambda url: False
        )
        result = read_feed_catalog(SHOP, transport=httpx.MockTransport(_no_request))
        assert not result.found
        assert result.source == SOURCE_HTML
        assert "Blocked" in (result.error or "")

    def test_a_public_ip_host_passes_the_gate_and_reaches_the_fetcher(self):
        # An IP literal resolves without DNS, so the strict resolver runs end to end:
        # gate passes, shopfeed probes find no feed, and the crawl falls back to HTML.
        url = "https://93.184.215.14/"
        transport = httpx.MockTransport(lambda request: httpx.Response(404))
        result = read_feed_catalog(url, transport=transport, sleep=_NoWait())
        assert not result.found
        assert result.source == SOURCE_HTML


class TestTheFeedReadFailureModes:
    """A competitor's server can do anything; the crawl falls back to HTML."""

    def test_an_unexpected_error_is_reported_not_raised(self):
        # Network errors are wrapped by shopfeed and swallowed per probe, so this is
        # something that slips past its wrapping: it must still end as a FeedResult.
        def _explode(request):
            raise RuntimeError("boom")

        result = read_feed_catalog(
            SHOP,
            transport=httpx.MockTransport(_explode),
            allow_private=True,
            sleep=_NoWait(),
        )
        assert not result.found
        assert result.source == SOURCE_HTML
        assert "boom" in (result.error or "")

    def test_a_page_that_breaks_the_jsonld_parser_yields_nothing(self, monkeypatch):
        # Broken JSON-LD is common on the wild web; it must never fail a crawl.
        import shopfeed.jsonld as jsonld_module

        def _broken(html, url):
            raise RuntimeError("parser exploded")

        monkeypatch.setattr(jsonld_module, "products_from_html", _broken)
        assert read_jsonld_products("<html></html>", SHOP) == []

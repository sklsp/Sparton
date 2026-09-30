"""Read a competitor's catalog from the shop's own data instead of the page.

Most webshops already publish their catalog as machine-readable data: Shopify
and WooCommerce expose a public JSON API, and almost every platform embeds
schema.org JSON-LD in each product page. That data is *exact* -- the price is
the number the shop charges, not a number we inferred from markup -- and
reading it costs no tokens.

So the crawl tries, in order:

    1. the platform feed   (Shopify / WooCommerce)  -> data_source="feed"
    2. JSON-LD via sitemap, or on a product page     -> data_source="jsonld"
    3. the existing HTML crawl                        -> data_source="html"

Only the third path can call the LLM, and it is the only path that produces a
price we are guessing at. The tier that won is recorded on every capture, so
the UI can tell the customer which kind of number they are looking at instead
of implying all three are equally trustworthy.

Why this is worth doing at all: it is simultaneously more accurate and cheaper.
Extraction from rendered HTML has to guess (which of these six numbers is the
price? is the struck-through one a "was" price or a second variant?), and it
costs tokens on every crawled page. A feed is a single request for the whole
catalog, with no guessing and no tokens.

**SSRF: Sparton's own guard is still the outer gate.** `shopfeed.net` has its
own checks -- it refuses non-public addresses on every redirect hop and verifies
the connected peer, so DNS rebinding does not get past it either -- but the
decision to crawl a URL at all is made by Sparton, before shopfeed is called.
Defence in depth: the inner library is a dependency we did not write, so the
outer check is the one whose absence would be our bug.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.core.observability.metrics import inc
from app.research.extraction import ExtractedProduct

logger = logging.getLogger(__name__)

#: Data sources, in descending order of exactness. The values are stored in the
#: database and shown to the customer, so they are part of the product's
#: vocabulary, not an internal detail.
SOURCE_FEED = "feed"
SOURCE_JSONLD = "jsonld"
SOURCE_HTML = "html"

#: Human wording for the UI. "Exact" is the operative word: a feed price is the
#: price the shop charges, with no interpretation applied.
SOURCE_LABELS = {
    SOURCE_FEED: "exact prices from the shop''s feed",
    SOURCE_JSONLD: "exact prices from the page''s structured data",
    SOURCE_HTML: "extracted from the page",
}

#: Confidence attached to each source. A feed is the shop's own numbers; a
#: parsed page is our best reading of a rendering.
SOURCE_CONFIDENCE = {SOURCE_FEED: 1.0, SOURCE_JSONLD: 0.95, SOURCE_HTML: 0.6}


def source_label(source: str) -> str:
    """Customer-facing wording for a data source."""
    return SOURCE_LABELS.get(source or SOURCE_HTML, SOURCE_LABELS[SOURCE_HTML])

# --- shopfeed plumbing --------------------------------------------------------
# shopfeed is a local, private dependency (see requirements.txt). It is optional
# at runtime: if it is not installed the product falls back to the HTML crawl,
# which is slower and less exact but still correct. An ImportError must never
# take a customer's crawl down.


def shopfeed_available() -> bool:
    """True when the feed reader can be used. Never raises."""
    try:
        import shopfeed.catalog  # noqa: F401
        import shopfeed.jsonld  # noqa: F401

        return True
    except ImportError:
        return False


def _fetcher(*, allow_private: bool, transport: Any | None = None):
    """A shopfeed Fetcher wired to our politeness and size limits.

    `resolve` is left at shopfeed's own default, which is the strict one: only
    globally routable addresses. Sparton's crawler has an escape hatch for local
    development fixtures, and this honours it, because a test that cannot reach
    a loopback fixture cannot test the feed path at all.

    robots.txt is honoured. A shop that disallows the feed gets the HTML crawl
    instead, which asks the same question again -- and is also refused. That is
    the correct outcome, not a bug to work around.
    """
    from shopfeed.net import Fetcher

    return Fetcher(
        min_interval=1.0,
        max_bytes=8_000_000,
        max_redirects=5,
        timeout=20.0,
        transport=transport,
        # The peer-address check in shopfeed needs a real socket, which a mock
        # transport does not have, so it is a no-op there by design.
        sleep=_no_sleep,
        # shopfeed refuses any name that does not resolve to a globally routable
        # address, which is the right default and is left alone in production.
        # The escape hatch is for local development fixtures and tests, where
        # there is no public DNS to resolve: it matches the same flag our own
        # crawler uses, so "can I crawl this locally" has one answer, not two.
        resolve=(_allow_any if allow_private else _public_only),
    )


def _no_sleep(_seconds: float) -> None:
    """Politeness is enforced by our caller's throttle; do not double-wait."""


def _allow_any(_hostname: str) -> bool:
    return True


def _public_only(_hostname: str) -> bool:
    """shopfeed's own strict resolver: globally routable addresses only."""
    from shopfeed.net import _public

    return _public(_hostname)


def _decimal(value: Decimal | None) -> float | None:
    """Decimal -> float for our Float column, refusing NaN/Inf.

    A NaN in a Float column is the kind of value that compares unequal to
    itself and quietly poisons a diff. Reject it here rather than store it.
    """
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if as_float != as_float or as_float in (float("inf"), float("-inf")):
        return None
    return round(as_float, 4)


def _to_extracted(product, *, source: str) -> ExtractedProduct | None:
    """One shopfeed Product -> our ExtractedProduct.

    Returns None when there is nothing to store: no title and no price is not a
    product, and writing a row for it would show up as a phantom "product
    removed" on the next crawl.
    """
    price = _decimal(product.price)
    if not product.title and price is None:
        return None
    return ExtractedProduct(
        name=product.title or "Untitled product",
        brand=product.vendor or "",
        category="",
        price=price,
        currency=(product.currency or "").upper()[:8] or "EUR",
        availability="in_stock" if product.available else "out_of_stock",
        description="",
        image_url=product.image or "",
        url=product.url or "",
        # `compare_at` is the number a markdown ended at. schema.org has no
        # standard for it, so JSON-LD rows legitimately carry None.
        attributes={
            "sku": product.sku or "",
            "external_id": product.id or "",
            "compare_at": _decimal(product.compare_at),
            "variants": product.variants or 1,
            "gtin": product.gtin or "",
            "data_source": source,
        },
        method=source,
        confidence=SOURCE_CONFIDENCE[source],
    )

# --- the entry points ---------------------------------------------------------


@dataclass(slots=True)
class FeedResult:
    """What a feed read produced, and which tier produced it."""

    products: list[ExtractedProduct]
    source: str = SOURCE_HTML
    platform: str | None = None
    truncated: bool = False
    error: str = ""

    @property
    def found(self) -> bool:
        return bool(self.products)


def read_feed_catalog(
    url: str,
    *,
    max_products: int = 200,
    transport: Any | None = None,
    allow_private: bool = False,
) -> FeedResult:
    """Try the platform feed, then JSON-LD via the sitemap.

    Returns an empty result with `source="html"` when neither yields anything,
    which is the caller's signal to run the ordinary crawl. Never raises: a
    competitor with no feed is the normal case, not an error.
    """
    if not shopfeed_available():
        return FeedResult([], SOURCE_HTML, error="shopfeed is not installed")
    try:
        from shopfeed import catalog as shopfeed_catalog
    except ImportError as exc:  # pragma: no cover - availability checked above
        return FeedResult([], SOURCE_HTML, error=str(exc))

    if not allow_private and not _sparton_allows(url):
        return FeedResult(
            [], SOURCE_HTML, error="Blocked: Sparton's own SSRF guard refused the URL"
        )

    fetcher = _fetcher(allow_private=allow_private, transport=transport)
    try:
        # `sitemap_pages=0` on the platform path: the feed is the cheap answer,
        # and the sitemap tier is a different function's job (below). Leaving it
        # on would make a Shopify shop pay for a full site walk before we had
        # even looked at products.json.
        result = shopfeed_catalog.read_catalog(
            url,
            fetcher=fetcher,
            max_products=max_products,
            sitemap_pages=0,
        )
    except Exception as exc:  # noqa: BLE001 - a competitor must not fail the week
        logger.info("Feed read failed for %s: %s", url, exc)
        return FeedResult([], SOURCE_HTML, error=str(exc)[:200])
    finally:
        fetcher.close()

    if result.platform is None or not result.products:
        return FeedResult([], SOURCE_HTML, platform=result.platform)

    # A sitemap-derived catalog reports platform "jsonld"; a real feed reports
    # "shopify" or "woocommerce". Both are exact, but the customer-facing claim
    # is not the same: one is the shop's API, the other is markup on their pages.
    source = SOURCE_JSONLD if result.platform == "jsonld" else SOURCE_FEED
    products = [
        converted
        for converted in (_to_extracted(p, source=source) for p in result.products)
        if converted is not None
    ]
    inc("competitor_feed_products_total", source=source)
    return FeedResult(products, source, result.platform, result.truncated)


def _sparton_allows(url: str) -> bool:
    """Sparton's own SSRF gate, applied before the feed reader is called.

    This is the outer gate. shopfeed has equivalent checks of its own, but it is
    a dependency we did not write: if it is ever swapped, replaced, or found to
    be less strict than advertised, this is the check that still holds.
    """
    from app.research.crawler import _is_public_http_url

    if settings.crawler_allow_private_addresses:
        return True
    return _is_public_http_url(url)


def read_jsonld_products(
    html: str, url: str
) -> list[ExtractedProduct]:
    """Exact products from a page's schema.org markup, or nothing.

    Called on a product page the HTML crawl already fetched, so it costs no
    extra request. When it returns a product we skip LLM extraction entirely,
    which is the whole point: exact numbers, no tokens.
    """
    if not html or not shopfeed_available():
        return []
    try:
        from shopfeed import jsonld as shopfeed_jsonld
    except ImportError:  # pragma: no cover
        return []
    try:
        raw = shopfeed_jsonld.products_from_html(html, url)
    except Exception as exc:  # noqa: BLE001 - broken JSON-LD is common, not fatal
        logger.info("JSON-LD read failed for %s: %s", url, exc)
        return []
    return [
        converted
        for converted in (_to_extracted(p, source=SOURCE_JSONLD) for p in raw)
        if converted is not None
    ]


__all__ = [
    "FeedResult",
    "SOURCE_FEED",
    "SOURCE_HTML",
    "SOURCE_JSONLD",
    "SOURCE_LABELS",
    "read_feed_catalog",
    "read_jsonld_products",
    "shopfeed_available",
    "source_label",
]

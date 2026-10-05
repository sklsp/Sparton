"""URL normalisation, platform detection, and the SSRF pre-check.

This module is the front door. A customer pastes `my-shop.myshopify.com` or
`https://www.myshop.com/collections/all?page=2` and we have to answer three
questions before anything else happens:

1. Is this a URL we are willing to fetch at all? (scheme + SSRF guard)
2. What is the canonical domain? (the identity key for a shop or competitor)
3. What platform is it? (Shopify, WooCommerce, Bol.com, ...)

Platform detection matters because each platform has a *cheap, structured* way
to enumerate a catalogue. Shopify exposes `/products.json`, WooCommerce exposes
`/wp-json/wc/store/v1/products`. Using them instead of crawling listing pages is
the difference between 1 request and 40, and it is far more accurate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from app.core.database.ecommerce_models import Platform
from app.research.crawler import _is_public_http_url

#: A registrable-ish domain label. Deliberately permissive: it only has to be
#: good enough to key a row and to build a robots.txt URL.
_DOMAIN_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)

MAX_URL_LENGTH = 500


class InvalidShopUrl(ValueError):
    """A URL we will not accept. Carries a message safe to show a customer."""


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    """A URL that passed every check, reduced to the parts we store."""

    url: str
    domain: str
    platform: str

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "domain": self.domain, "platform": self.platform}


#: Hostname fragments that identify a platform. Checked in order, so the more
#: specific patterns come first.
_PLATFORM_HOST_HINTS: tuple[tuple[str, str], ...] = (
    ("myshopify.", Platform.SHOPIFY),
    (".myshopify.", Platform.SHOPIFY),
    ("bol.com", Platform.BOL),
    ("bol.nl", Platform.BOL),
    ("bigcommerce", Platform.BIGCOMMERCE),
    ("prestashop", Platform.PRESTASHOP),
    ("magento", Platform.MAGENTO),
)

#: Path/query fingerprints. A WooCommerce site exposes /wp-json, so we look for
#: those markers before falling back to "unknown" and letting the generic
#: crawler handle it.
_PLATFORM_PATH_HINTS: tuple[tuple[str, str], ...] = (
    ("/wp-json/", Platform.WOOCOMMERCE),
    ("/wp-content/plugins/woocommerce", Platform.WOOCOMMERCE),
    ("/collections/", Platform.SHOPIFY),
    ("/products.json", Platform.SHOPIFY),
)


def normalize_url(raw: str) -> NormalizedUrl:
    """Validate, canonicalise, and identify a storefront URL.

    Raises :class:`InvalidShopUrl` with a customer-readable message. The SSRF
    check is the important one: this is the value the crawler will later be
    pointed at, so a customer must not be able to aim it at the cloud metadata
    endpoint or an internal service (D-016).
    """
    if raw is None:
        raise InvalidShopUrl("A shop URL is required")
    candidate = raw.strip()
    if not candidate:
        raise InvalidShopUrl("A shop URL is required")
    if len(candidate) > MAX_URL_LENGTH:
        raise InvalidShopUrl(f"URL is too long (max {MAX_URL_LENGTH} characters)")

    # Accept "example.com" as well as "https://example.com".
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise InvalidShopUrl("That does not look like a valid URL") from exc

    if parsed.scheme.lower() not in ("http", "https"):
        raise InvalidShopUrl("Only http and https URLs are supported")

    host = (parsed.hostname or "").lower()
    if not host:
        raise InvalidShopUrl("That URL has no hostname")
    if not _DOMAIN_RE.match(host):
        raise InvalidShopUrl(f"'{host}' is not a valid domain name")
    if "." not in host:
        raise InvalidShopUrl("Enter a full domain, for example my-shop.com")

    # Drop default ports and the fragment; keep a real path and query.
    netloc = host
    if parsed.port and parsed.port not in (80, 443):
        netloc = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    canonical = urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))

    # The SSRF guard the crawler uses, applied *before* the URL is stored. It
    # resolves DNS and refuses private/loopback/link-local/reserved targets.
    from app.core.config import settings

    if not settings.crawler_allow_private_addresses and not _is_public_http_url(canonical):
        raise InvalidShopUrl(
            "That address is not a public website we can access. "
            "Enter the public URL of your shop."
        )

    return NormalizedUrl(
        url=canonical,
        domain=host,
        platform=detect_platform(canonical, host),
    )


def detect_platform(url: str, host: str | None = None) -> str:
    """Best-effort platform identification from the URL alone.

    Deliberately conservative: a wrong guess costs one wasted request, whereas
    guessing "unknown" just means we fall back to crawling listing pages.
    """
    parsed = urlparse(url)
    host = (host or parsed.hostname or "").lower()
    full = f"{host}{parsed.path}".lower()

    for fragment, platform in _PLATFORM_HOST_HINTS:
        if fragment in host:
            return platform
    for fragment, platform in _PLATFORM_PATH_HINTS:
        if fragment in full:
            return platform
    return Platform.UNKNOWN


def base_url(url: str) -> str:
    """`https://shop.example/collections/all?x=1` -> `https://shop.example`."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), "", "", "", ""))


def product_feed_urls(url: str, platform: str) -> list[str]:
    """Structured catalogue endpoints to try before falling back to crawling.

    Shopify's `/products.json` returns the whole catalogue paginated, which turns
    a 40-page crawl into 3 requests and gives exact prices. WooCommerce's store
    API is the same idea. Both are public, unauthenticated, and intended for
    this use.
    """
    root = base_url(url)
    if platform == Platform.SHOPIFY:
        return [f"{root}/products.json?limit=250&page=1"]
    if platform == Platform.WOOCOMMERCE:
        return [f"{root}/wp-json/wc/store/v1/products?per_page=100&page=1"]
    return []


def robots_url_for(url: str) -> str:
    return f"{base_url(url)}/robots.txt"


__all__ = [
    "InvalidShopUrl",
    "NormalizedUrl",
    "base_url",
    "detect_platform",
    "normalize_url",
    "product_feed_urls",
    "robots_url_for",
]

"""Competitor discovery.

Two ways a competitor enters a customer's watchlist:

1. The customer pastes a URL. Always allowed.
2. We search for shops that look like they sell the same thing, and propose
   them for confirmation.

Search results are *untrusted input*. They arrive as URLs from a third party and
they are used to point the crawler, so every candidate goes through the same
SSRF guard the crawler itself uses (docs/DECISIONS.md D-016) before it is even
stored. A discovered competitor is stored with `confidence < 1` and
`discovery_method="search"` so the UI can present it as a suggestion rather
than a fact.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database.ecommerce_models import Competitor, Platform, Shop
from app.core.database.models import utcnow
from app.ecommerce.urls import InvalidShopUrl, normalize_url
from app.research.crawler import _is_public_http_url
from app.research.discovery import DuckDuckGoProvider, SearchProvider

logger = logging.getLogger(__name__)

#: Hosts that are never a competitor: search engines, social platforms, and
#: the marketplaces' own infrastructure. A customer pasting one of these has
#: made a mistake, not discovered a rival.
_BLOCKED_HOST_SUBSTRINGS = (
    "google.", "bing.", "duckduckgo.", "yahoo.", "baidu.",
    "facebook.", "instagram.", "tiktok.", "pinterest.", "reddit.",
    "youtube.", "linkedin.", "twitter.", "x.com",
    "amazon.", "ebay.", "etsy.", "alibaba.", "aliexpress.",
    "wikipedia.", "amazon.",
)

#: Suffixes a hosting platform appends to every tenant. `acme.myshopify.com`
#: and `claybarn.myshopify.com` are different shops that happen to share a
#: suffix, so the label in front of it is the real owner key.
_PLATFORM_SUFFIXES = frozenset({
    "myshopify.com", "bigcommerce.com", "prestashop.com", "shopify.com",
    "magento.com", "woocommerce.com", "squarespace.com", "wixsite.com",
    "weebly.com", "jimdo.com", "tumblr.com", "github.io", "netlify.app",
})

#: Public suffixes that are two labels rather than one. A short, hardcoded list
#: covering what European e-commerce actually uses — a full Public Suffix List
#: is a multi-megabyte dependency for no benefit here.
_TWO_PART_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk",
    "com.au", "net.au", "org.au", "co.nz", "com.br", "com.mx", "com.ar",
    "co.za", "com.tr", "com.pl", "com.es", "com.pt", "com.gr", "co.jp",
    "com.sg", "com.hk", "co.in", "com.cn",
})

#: Noise prefixes stripped before comparing two domains for "same owner".
_SUBDOMAIN_STRIP = ("www.", "shop.", "store.", "web.", "m.")


class ShopError(ValueError):
    """A shop or competitor could not be added. Safe to show a customer."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _is_blocked_host(domain: str) -> bool:
    lowered = domain.lower()
    return any(fragment in lowered for fragment in _BLOCKED_HOST_SUBSTRINGS)


def shop_already_tracked(db: Session, organization_id: int | None, domain: str) -> bool:
    """Has this tenant already added this domain?

    Checked by the API *before* the plan limit, so a customer re-adding a shop
    is told "you already have it" rather than "upgrade your plan" — the second
    message is both wrong and a way to make a limit feel arbitrary (D-009).
    """
    return db.execute(
        select(Shop.id).where(
            Shop.organization_id == organization_id, Shop.domain == domain
        )
    ).first() is not None


def _registrable(domain: str) -> str:
    """Reduce a hostname to a stable owner key, for "is this the same shop?".

    Naively taking the last two labels is wrong for the platforms this product
    targets: `acme.myshopify.com` and `claybarn.myshopify.com` both end in
    `myshopify.com`, so every Shopify customer would look like a competitor of
    every other one. Shared platform suffixes are therefore skipped and the
    label in front of them is used instead.

    The fallback is a hardcoded list of common two-part public suffixes, which
    covers the cases this product actually meets without pulling in a
    dependency or a PSL snapshot.
    """
    host = (domain or "").lower().strip(".")
    for prefix in _SUBDOMAIN_STRIP:
        if host.startswith(prefix):
            host = host[len(prefix):]

    labels = host.split(".")
    if len(labels) <= 2:
        return host

    # A platform suffix: skip it and take the label in front, which is the
    # tenant identifier Shopify/BigCommerce/PrestaShop assign per store.
    if ".".join(labels[-2:]) in _PLATFORM_SUFFIXES:
        return labels[-3] if len(labels) >= 3 else ".".join(labels[-2:])
    if labels[-1].isdigit() and len(labels) >= 3:
        return ".".join(labels[-3:-1])  # IP-style or CDN shard

    if ".".join(labels[-2:]) in _TWO_PART_SUFFIXES:
        return ".".join(labels[-3:]) if len(labels) >= 3 else host
    return ".".join(labels[-2:])


def add_shop(
    db: Session,
    organization_id: int | None,
    *,
    url: str,
    name: str = "",
    category: str = "",
    currency: str = "EUR",
    crawl_frequency_hours: int = 168,
) -> Shop:
    """Register a customer's own storefront.

    The URL goes through the full validation and SSRF guard before it is
    stored, because it is the value the crawler will be pointed at.
    """
    try:
        normalized = normalize_url(url)
    except InvalidShopUrl as exc:
        raise ShopError(str(exc)) from exc

    # Checked before the plan limit by the caller, so a re-added shop is
    # reported as a duplicate rather than as an upsell.
    if db.execute(
        select(Shop.id).where(
            Shop.organization_id == organization_id,
            Shop.domain == normalized.domain,
        )
    ).first() is not None:
        raise ShopError("You have already added this shop", status_code=409)

    # A shop with a 2-hour crawl budget would be an abuse vector against other
    # people's servers, so the floor is 6 hours whatever the plan allows.
    interval = max(6, min(int(crawl_frequency_hours or 168), 24 * 30))

    shop = Shop(
        organization_id=organization_id,
        name=(name or normalized.domain).strip()[:200],
        url=normalized.url,
        domain=normalized.domain,
        platform=normalized.platform,
        category=(category or "").strip()[:120],
        currency=(currency or "EUR").upper()[:8],
        crawl_frequency_hours=interval,
        is_active=True,
        next_crawl_at=utcnow(),
    )
    db.add(shop)
    db.commit()
    db.refresh(shop)
    return shop


def add_competitor(
    db: Session,
    organization_id: int | None,
    *,
    url: str,
    name: str = "",
    shop_id: int | None = None,
    discovery_method: str = "manual",
    confidence: float = 1.0,
) -> Competitor:
    """Add a competitor by URL. This is the primary onboarding path."""
    try:
        normalized = normalize_url(url)
    except InvalidShopUrl as exc:
        raise ShopError(str(exc)) from exc

    if _is_blocked_host(normalized.domain):
        raise ShopError(
            "That is not a shop — it looks like a search engine or marketplace. "
            "Enter the competitor's own store URL."
        )

    if shop_id is not None:
        owner = db.execute(
            select(Shop).where(Shop.id == shop_id, Shop.organization_id == organization_id)
        ).scalars().first()
        if owner is None:
            # 404, not 403: do not confirm that another tenant's shop exists.
            raise ShopError("Shop not found", status_code=404)
        if _registrable(owner.domain) == _registrable(normalized.domain):
            raise ShopError("That is your own shop, not a competitor")

    existing = db.execute(
        select(Competitor).where(
            Competitor.organization_id == organization_id,
            Competitor.domain == normalized.domain,
        )
    ).scalars().first()
    if existing is not None:
        raise ShopError("You are already tracking this competitor", status_code=409)

    competitor = Competitor(
        organization_id=organization_id,
        shop_id=shop_id,
        domain=normalized.domain,
        name=(name or normalized.domain).strip()[:200],
        url=normalized.url,
        platform=normalized.platform,
        discovery_method=discovery_method,
        confidence=max(0.0, min(float(confidence), 1.0)),
        is_active=True,
    )
    db.add(competitor)
    db.commit()
    db.refresh(competitor)
    return competitor


# ---------------------------------------------------------------------------
# Search-based suggestions
# ---------------------------------------------------------------------------
def suggest_competitors(
    db: Session,
    organization_id: int | None,
    shop: Shop,
    *,
    limit: int | None = None,
    search: SearchProvider | None = None,
) -> list[dict[str, Any]]:
    """Propose competitors for a shop, without adding any of them.

    Returns *candidates*, not rows. The customer confirms before anything is
    stored, because auto-adding a competitor means we start crawling someone
    else's server on a guess. Every candidate is SSRF-checked and filtered
    against the customer's own domain, the already-tracked list, and the
    blocked-host list.
    """
    cap = min(limit or settings.competitor_discovery_limit, 25)
    query_terms = " ".join(
        part for part in (shop.category.strip(), shop.name.strip()) if part
    ) or shop.domain
    search_query = f"{query_terms} shop online store".strip()

    provider = search or DuckDuckGoProvider()
    owns = search is None
    try:
        urls = provider.search(search_query, limit=cap * 3)
    except Exception as exc:  # noqa: BLE001 — a search outage is not an error
        logger.warning("Competitor search failed for %s: %s", shop.domain, exc)
        return []
    finally:
        if owns and hasattr(provider, "close"):
            provider.close()

    own = _registrable(shop.domain)
    tracked = {
        row.domain
        for row in db.execute(
            select(Competitor.domain).where(
                Competitor.organization_id == organization_id
            )
        ).scalars()
    }

    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in urls:
        if len(suggestions) >= cap:
            break
        host = (urlparse(url).hostname or "").lower()
        if not host or host in seen:
            continue
        if _is_blocked_host(host) or _registrable(host) == own or host in tracked:
            continue
        # Same SSRF guard as everywhere else: a search result is untrusted input
        # and this value is destined for the crawler.
        if not _is_public_http_url(url):
            continue
        seen.add(host)
        suggestions.append(
            {
                "domain": host,
                "url": url,
                "platform": normalize_url(url).platform,
                "reason": f"Matches '{search_query}'",
                # Search relevance is not evidence. The customer confirms.
                "confidence": 0.4,
            }
        )
    return suggestions

__all__ = ["changes", "crawl", "discovery", "reports"]

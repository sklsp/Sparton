"""The customer's own catalogue, and which competitor products are the same.

Closes D-031. Two halves:

* **Reading the own shop.** Exactly like a competitor: `read_feed_catalog`, the
  shop's own feed or structured data. There is deliberately no HTML fallback
  here. A guessed "your price" next to an exact competitor price would make the
  comparison look more certain than it is; no line is more honest than a wrong one.
* **Pairing products.** `shopfeed.match`: a shared barcode decides, otherwise
  titles under two hard rules (every number agrees, so "200g" never pairs with
  "400g"; the brand agrees when both sides name one). One-to-one per competitor,
  rebuilt from the latest captures after every crawl.

The gap ("they are 10.0% cheaper than you") is Decimal arithmetic on stored
prices. Only a barcode match is ever called certain (`MatchConfidence`).
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database.ecommerce_models import (
    ChangeEvent,
    Competitor,
    CompetitorProduct,
    ProductMatch,
    Shop,
    ShopProduct,
)
from app.core.database.models import utcnow
from app.ecommerce.feeds import read_feed_catalog, shopfeed_available

logger = logging.getLogger(__name__)

#: Same page budget a competitor read gets (crawl.DEFAULT_MAX_PAGES).
DEFAULT_MAX_PRODUCTS = 40
_TENTH = Decimal("0.1")


def price_gap(own: Decimal | None, theirs: Decimal | None) -> Decimal | None:
    """How much cheaper (+) or dearer (-) they are than you, as % of your price.

    `None` when there is nothing honest to say: no price on either side, or a
    zero own price (a percentage of nothing).
    """
    if own is None or theirs is None or own <= 0:
        return None
    return ((own - theirs) / own * 100).quantize(_TENTH, rounding=ROUND_HALF_UP)


# --- reading the own shop -----------------------------------------------------


def read_own_catalog(
    db: Session,
    shop: Shop,
    *,
    transport: Any | None = None,
    allow_private: bool | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Capture the shop's own catalogue. Never raises for an unreadable shop."""
    feed = read_feed_catalog(
        shop.url or f"https://{shop.domain}",
        max_products=settings.crawler_max_pages_per_shop or DEFAULT_MAX_PRODUCTS,
        transport=transport,
        allow_private=(
            settings.crawler_allow_private_addresses if allow_private is None else allow_private
        ),
        sleep=sleep,
    )
    if not feed.found:
        return {"products": 0, "data_source": "", "error": feed.error or "no feed or structured data"}
    captured_at = utcnow()
    for product in feed.products:
        attrs = product.attributes
        db.add(
            ShopProduct(
                organization_id=shop.organization_id,
                shop_id=shop.id,
                captured_at=captured_at,
                source_url=product.url or shop.url,
                external_id=str(attrs.get("external_id") or attrs.get("sku") or "")[:160],
                name=(product.name or "")[:400],
                brand=(product.brand or "")[:200],
                gtin=str(attrs.get("gtin") or "")[:32],
                price=product.price,
                currency=product.currency or "EUR",
                in_stock=product.availability == "in_stock",
                data_source=feed.source,
            )
        )
    db.commit()
    return {"products": len(feed.products), "data_source": feed.source, "error": ""}


# --- pairing ------------------------------------------------------------------


def _latest(db: Session, model, owner_col, owner_id: int) -> list:
    """Every row of the most recent capture for one shop or competitor."""
    last = db.execute(select(func.max(model.captured_at)).where(owner_col == owner_id)).scalar()
    if last is None:
        return []
    return list(
        db.execute(select(model).where(owner_col == owner_id, model.captured_at == last)).scalars()
    )


def _as_product(row):
    from shopfeed.catalog import Product

    return Product(
        platform="",
        id=str(row.id),
        title=row.name or "",
        url=row.source_url or "",
        price=row.price,
        compare_at=None,
        currency=row.currency,
        available=bool(row.in_stock),
        vendor=row.brand or "",
        gtin=row.gtin or "",
    )


def rematch(db: Session, shop: Shop) -> int:
    """Rebuild the shop's matches from the latest captures. Returns how many.

    Needs shopfeed (it is the matcher). Without it the old matches are left as
    they are rather than wiped: stale pairs beat silently losing them.
    """
    if not shopfeed_available():
        return 0
    from shopfeed.match import match

    own = [_as_product(r) for r in _latest(db, ShopProduct, ShopProduct.shop_id, shop.id)]
    competitors = db.execute(
        select(Competitor).where(
            Competitor.organization_id == shop.organization_id, Competitor.shop_id == shop.id
        )
    ).scalars()
    now, total = utcnow(), 0
    for competitor in competitors:
        db.execute(
            delete(ProductMatch).where(
                ProductMatch.shop_id == shop.id, ProductMatch.competitor_id == competitor.id
            )
        )
        theirs = [
            _as_product(r)
            for r in _latest(db, CompetitorProduct, CompetitorProduct.competitor_id, competitor.id)
        ]
        for m in match(own, theirs) if own and theirs else []:
            db.add(
                ProductMatch(
                    organization_id=shop.organization_id,
                    shop_id=shop.id,
                    competitor_id=competitor.id,
                    own_url=m.mine.url,
                    own_name=m.mine.title[:400],
                    competitor_url=m.theirs.url,
                    competitor_name=m.theirs.title[:400],
                    method=m.by,
                    score=float(m.score),
                    matched_at=now,
                )
            )
            total += 1
    db.commit()
    return total


# --- reading matches back -----------------------------------------------------


def _own_at(points: list[ShopProduct], when: datetime | None) -> ShopProduct | None:
    """The own reading in force at `when`: the last one taken at or before it."""
    if when is None:
        return points[-1] if points else None
    when = when.replace(tzinfo=None)
    found = None
    for p in points:
        if p.captured_at.replace(tzinfo=None) <= when:
            found = p
    return found


def own_history(db: Session, match: ProductMatch, limit: int = 500) -> list[ShopProduct]:
    """Every capture of the matched own product, oldest first."""
    rows = db.execute(
        select(ShopProduct)
        .where(
            ShopProduct.organization_id == match.organization_id,
            ShopProduct.shop_id == match.shop_id,
            ShopProduct.source_url == match.own_url,
        )
        .order_by(ShopProduct.captured_at.desc())
        .limit(limit)
    ).scalars()
    return list(reversed(list(rows)))


def match_for(db: Session, organization_id: int | None, competitor_id: int | None, url: str):
    if competitor_id is None or not url:
        return None
    return db.execute(
        select(ProductMatch).where(
            ProductMatch.organization_id == organization_id,
            ProductMatch.competitor_id == competitor_id,
            ProductMatch.competitor_url == url,
        )
    ).scalars().first()


def comparisons(
    db: Session, organization_id: int | None, changes: list[ChangeEvent]
) -> dict[int, dict[str, Any]]:
    """`{change_id: comparison}` for the changes whose product has a match.

    The competitor's price is the one the change reports (`new_price`); the own
    price is the reading in force when the change was detected, so both sides of
    the gap are from the same moment.
    """
    priced = [c for c in changes if c.competitor_id and c.source_url and c.new_price is not None]
    if not priced:
        return {}
    matches = db.execute(
        select(ProductMatch).where(
            ProductMatch.organization_id == organization_id,
            ProductMatch.competitor_id.in_({c.competitor_id for c in priced}),
            ProductMatch.competitor_url.in_({c.source_url for c in priced}),
        )
    ).scalars().all()
    by_key = {(m.competitor_id, m.competitor_url): m for m in matches}
    if not by_key:
        return {}
    own_rows = db.execute(
        select(ShopProduct)
        .where(
            ShopProduct.organization_id == organization_id,
            ShopProduct.shop_id.in_({m.shop_id for m in matches}),
            ShopProduct.source_url.in_({m.own_url for m in matches}),
        )
        .order_by(ShopProduct.captured_at)
    ).scalars().all()
    history: dict[tuple[int, str], list[ShopProduct]] = {}
    for row in own_rows:
        history.setdefault((row.shop_id, row.source_url), []).append(row)

    out: dict[int, dict[str, Any]] = {}
    for change in priced:
        m = by_key.get((change.competitor_id, change.source_url))
        if m is None:
            continue
        own = _own_at(history.get((m.shop_id, m.own_url), []), change.detected_at)
        same_currency = own is not None and own.currency == change.currency
        out[change.id] = {
            "own_product": m.own_name,
            "own_url": m.own_url,
            "own_price": own.price if same_currency else None,
            "gap_pct": price_gap(own.price, change.new_price) if same_currency else None,
            "confidence": m.confidence,
            "method": m.method,
        }
    return out


__all__ = [
    "comparisons",
    "match_for",
    "own_history",
    "price_gap",
    "read_own_catalog",
    "rematch",
]

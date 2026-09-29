"""Change detection — the part customers actually pay for.

**This module contains no LLM calls at all** (docs/DECISIONS.md D-029). It is
arithmetic over two captures. That is a deliberate product decision:

* The numbers cannot be hallucinated. "Acme dropped the linen runner from
  €34 to €27.50" is computed, not generated.
* It costs nothing per crawl. A weekly crawl of 15 competitors x 200 products
  makes 3,000 comparisons; asking a model to do that would dominate the
  per-customer cost.

The AI is only ever used later, in ``reports.py``, to *write prose about these
facts*.

A "change" is a comparison between the two most recent captures of the same
``(competitor, source_url)``. Every change carries an ``evidence_url`` so the
customer can click through and verify it themselves.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database.ecommerce_models import (
    ChangeEvent,
    ChangeKind,
    ChangeSeverity,
    Competitor,
    CompetitorProduct,
)
from app.core.database.models import utcnow

logger = logging.getLogger(__name__)

#: A price move smaller than this is noise (rounding, VAT display, scraping
#: jitter) and is not worth an alert. Either 1% or 0.50 in the currency.
PRICE_EPSILON_PCT = 1.0
PRICE_EPSILON_ABS = 0.5

#: Prices above this are almost certainly parsed wrong (a "from 1,299,999.00"
#: scraped off a banner) and are ignored rather than alerted on.
PLAUSIBLE_MAX_PRICE = 100_000.0

_NON_WORD = re.compile(r"[^a-z0-9]+")

_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def normalize_name(value: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    This is the match key across crawls. Two captures of the same product have
    the same URL *and* (usually) a similar title; the URL is authoritative and
    the name is the fallback for sites that rewrite URLs between crawls.
    """
    return _NON_WORD.sub(" ", (value or "").lower()).strip()


def _plausible_price(price: float | None) -> bool:
    return price is not None and 0 < price <= PLAUSIBLE_MAX_PRICE


def price_severity(delta_pct: float) -> str:
    """How much a customer should care.

    The thresholds are the product's, not the crawler's: a 30% cut is a
    different event from a 3% rounding wobble.
    """
    magnitude = abs(delta_pct)
    if magnitude >= 20:
        return ChangeSeverity.HIGH
    if magnitude >= 10:
        return ChangeSeverity.MEDIUM
    return ChangeSeverity.LOW


def money(value: float | None, currency: str) -> str:
    """`27.5, "EUR"` -> `€27.50`. Shared by the diff and the report writer."""
    if value is None:
        return "unknown"
    symbol = _CURRENCY_SYMBOLS.get((currency or "").upper(), "")
    formatted = f"{value:,.2f}"
    return f"{symbol}{formatted}" if symbol else f"{formatted} {(currency or '').upper()}"


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


@dataclass(slots=True)
class DetectedChange:
    """A change, before it becomes a row."""

    kind: str
    severity: str
    product_name: str
    source_url: str
    title: str
    summary: str
    previous_price: float | None = None
    new_price: float | None = None
    currency: str = "EUR"
    competitor_name: str = ""
    competitor_domain: str = ""
    competitor_product_id: int | None = None
    detail: dict[str, Any] | None = None

    @property
    def delta(self) -> float | None:
        if self.previous_price is None or self.new_price is None:
            return None
        return round(self.new_price - self.previous_price, 2)

    @property
    def delta_pct(self) -> float | None:
        if not self.previous_price or self.new_price is None:
            return None
        return round((self.new_price - self.previous_price) / self.previous_price * 100, 2)


def diff_captures(
    previous: CompetitorProduct,
    current: CompetitorProduct,
    *,
    competitor: Competitor | None = None,
) -> list[DetectedChange]:
    """Compare two captures of the same product. Returns zero or more changes.

    A pure function of its two arguments, which is what makes it exhaustively
    testable without a database, a network, or a clock.
    """
    name = current.name or previous.name or "A product"
    domain = competitor.domain if competitor else ""
    shop_name = (competitor.name if competitor else "") or domain
    currency = current.currency or previous.currency or "EUR"
    changes: list[DetectedChange] = []

    # --- price ---------------------------------------------------------
    old_price, new_price = previous.price, current.price
    if _plausible_price(old_price) and _plausible_price(new_price) and old_price != new_price:
        delta = new_price - old_price
        delta_pct = (delta / old_price) * 100 if old_price else 0.0
        # Ignore rounding wobble: it trains customers to ignore real alerts.
        meaningful = (
            abs(delta_pct) >= PRICE_EPSILON_PCT or abs(delta) >= PRICE_EPSILON_ABS
        )
        if meaningful:
            dropped = delta < 0
            changes.append(
                DetectedChange(
                    kind=ChangeKind.PRICE_DECREASE if dropped else ChangeKind.PRICE_INCREASE,
                    severity=price_severity(delta_pct),
                    product_name=name,
                    source_url=current.source_url,
                    previous_price=old_price,
                    new_price=new_price,
                    currency=currency,
                    competitor_name=shop_name,
                    competitor_domain=domain,
                    competitor_product_id=current.id,
                    title=(
                        f"{shop_name} {'cut' if dropped else 'raised'} the price of {name} "
                        f"by {abs(delta_pct):.0f}%"
                    ),
                    summary=(
                        f"{name} moved from {money(old_price, currency)} to "
                        f"{money(new_price, currency)} "
                        f"({'down' if dropped else 'up'} {money(abs(delta), currency)})."
                    ),
                    detail={
                        "delta": round(delta, 2),
                        "delta_pct": round(delta_pct, 2),
                        "previous_captured_at": _iso(previous.captured_at),
                        "captured_at": _iso(current.captured_at),
                    },
                )
            )

    # --- stock ---------------------------------------------------------
    if previous.in_stock and not current.in_stock:
        changes.append(
            DetectedChange(
                kind=ChangeKind.OUT_OF_STOCK,
                severity=ChangeSeverity.MEDIUM,
                product_name=name,
                source_url=current.source_url,
                new_price=new_price,
                currency=currency,
                competitor_name=shop_name,
                competitor_domain=domain,
                competitor_product_id=current.id,
                title=f"{shop_name} is out of stock on {name}",
                summary=f"{name} was available at the last crawl and is now unavailable.",
                detail={
                    "previous_availability": previous.availability,
                    "availability": current.availability,
                },
            )
        )
    elif not previous.in_stock and current.in_stock:
        changes.append(
            DetectedChange(
                kind=ChangeKind.BACK_IN_STOCK,
                severity=ChangeSeverity.LOW,
                product_name=name,
                source_url=current.source_url,
                new_price=new_price,
                currency=currency,
                competitor_name=shop_name,
                competitor_domain=domain,
                competitor_product_id=current.id,
                title=f"{shop_name} is back in stock on {name}",
                summary=f"{name} is available again at {money(new_price, currency)}.",
                detail={
                    "previous_availability": previous.availability,
                    "availability": current.availability,
                },
            )
        )

    return changes


def previous_capture(
    db: Session, competitor_id: int, source_url: str, before: datetime
) -> CompetitorProduct | None:
    """The most recent capture of this product strictly before `before`."""
    return db.execute(
        select(CompetitorProduct)
        .where(
            CompetitorProduct.competitor_id == competitor_id,
            CompetitorProduct.source_url == source_url,
            CompetitorProduct.captured_at < before,
        )
        .order_by(CompetitorProduct.captured_at.desc())
        .limit(1)
    ).scalars().first()


def latest_capture_by_name(
    db: Session, competitor_id: int, normalized_name: str, before: datetime
) -> CompetitorProduct | None:
    """Fallback match for sites that rewrite product URLs between crawls."""
    if not normalized_name:
        return None
    return db.execute(
        select(CompetitorProduct)
        .where(
            CompetitorProduct.competitor_id == competitor_id,
            CompetitorProduct.normalized_name == normalized_name,
            CompetitorProduct.captured_at < before,
        )
        .order_by(CompetitorProduct.captured_at.desc())
        .limit(1)
    ).scalars().first()


def is_new(previous: CompetitorProduct | None, current: CompetitorProduct) -> bool:
    """A product seen for the first time at this competitor."""
    return previous is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def already_recorded(
    db: Session, organization_id: int | None, change: DetectedChange, competitor_id: int
) -> bool:
    """Has this exact observation already been alerted?

    Two crawls of the same shop can run close together, and re-alerting the
    same price move twice is the fastest way to make a customer mute the
    product. Checked on (kind, competitor, url, from-price, to-price), which is
    precisely "the same observation".
    """
    existing = db.execute(
        select(ChangeEvent.id).where(
            ChangeEvent.organization_id == organization_id,
            ChangeEvent.kind == change.kind,
            ChangeEvent.competitor_id == competitor_id,
            ChangeEvent.source_url == change.source_url,
            ChangeEvent.previous_price == change.previous_price,
            ChangeEvent.new_price == change.new_price,
        ).limit(1)
    ).first()
    return existing is not None


def record_changes(
    db: Session,
    *,
    organization_id: int | None,
    shop_id: int | None,
    competitor_id: int,
    changes: list[DetectedChange],
    detected_at: datetime | None = None,
) -> list[ChangeEvent]:
    """Persist new changes, skipping ones already alerted. Returns what was new."""
    moment = detected_at or utcnow()
    created: list[ChangeEvent] = []
    for change in changes:
        if already_recorded(db, organization_id, change, competitor_id):
            continue
        row = ChangeEvent(
            organization_id=organization_id,
            shop_id=shop_id,
            competitor_id=competitor_id,
            competitor_product_id=change.competitor_product_id,
            kind=change.kind,
            severity=change.severity,
            product_name=change.product_name,
            source_url=change.source_url,
            # The evidence link IS the product page we read the price from.
            evidence_url=change.source_url,
            competitor_name=change.competitor_name,
            competitor_domain=change.competitor_domain,
            previous_price=change.previous_price,
            new_price=change.new_price,
            currency=change.currency,
            delta=change.delta,
            delta_pct=change.delta_pct,
            title=change.title,
            summary=change.summary,
            detail=change.detail or {},
            detected_at=moment,
        )
        db.add(row)
        created.append(row)
    if created:
        db.commit()
        for row in created:
            db.refresh(row)
    return created


def new_product_change(
    current: CompetitorProduct, *, competitor: Competitor | None = None
) -> DetectedChange:
    """A product we had not seen at this competitor before."""
    domain = competitor.domain if competitor else ""
    shop_name = (competitor.name if competitor else "") or domain
    return DetectedChange(
        kind=ChangeKind.NEW_PRODUCT,
        severity=ChangeSeverity.LOW,
        product_name=current.name or "A product",
        source_url=current.source_url,
        new_price=current.price,
        currency=current.currency or "EUR",
        competitor_name=shop_name,
        competitor_domain=domain,
        competitor_product_id=current.id,
        title=f"{shop_name} listed a new product: {current.name}",
        summary=(
            f"{current.name} is new at {shop_name}"
            + (f" at {money(current.price, current.currency or 'EUR')}" if current.price else "")
            + "."
        ),
        detail={"category": current.category, "first_seen": _iso(current.captured_at)},
    )


def removed_product_change(
    last: CompetitorProduct, *, competitor: Competitor | None = None
) -> DetectedChange:
    """A product that was in the previous crawl and is not in this one."""
    domain = competitor.domain if competitor else ""
    shop_name = (competitor.name if competitor else "") or domain
    return DetectedChange(
        kind=ChangeKind.REMOVED_PRODUCT,
        severity=ChangeSeverity.MEDIUM,
        product_name=last.name or "A product",
        source_url=last.source_url,
        previous_price=last.price,
        currency=last.currency or "EUR",
        competitor_name=shop_name,
        competitor_domain=domain,
        title=f"{shop_name} no longer lists {last.name}",
        summary=(
            f"{last.name} was in the catalogue at the last crawl and is gone now."
        ),
        detail={"last_seen": _iso(last.captured_at), "last_price": last.price},
    )

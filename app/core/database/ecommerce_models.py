"""Sparton Intelligence — the product schema.

This is the loop a customer pays for:

    add shop URL -> discover competitors -> crawl on a schedule
                 -> detect price/assortment/stock changes
                 -> weekly AI-written report + in-app alerts

Design notes that matter more than the column list:

* **Every tenant-owned row carries ``organization_id``.** No join can cross a
  tenant boundary, and every read path filters on it.
* **``competitor_products`` is a time series**, not a "current value" table
  (docs/DECISIONS.md D-008). A change is a comparison between two captures, and
  the evidence link for a price must be reproducible: *this* price, *at* this
  URL, *on* this date.
* **Change detection needs no LLM at all** (D-029). The diff engine is pure
  arithmetic over two snapshots; the AI only *writes the summary* of facts we
  already computed. That is what keeps the numbers non-hallucinatable and the
  cost per customer low.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import MONEY, Base, JSONType
from app.core.database.models import utcnow


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------
class Platform:
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    BOL = "bol"
    MAGENTO = "magento"
    BIGCOMMERCE = "bigcommerce"
    PRESTASHOP = "prestashop"
    UNKNOWN = "unknown"

    ALL = (SHOPIFY, WOOCOMMERCE, BOL, MAGENTO, BIGCOMMERCE, PRESTASHOP, UNKNOWN)


class CrawlStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"

    TERMINAL = frozenset({COMPLETED, PARTIAL, FAILED, BLOCKED})


class ChangeKind:
    PRICE_INCREASE = "price_increase"
    PRICE_DECREASE = "price_decrease"
    NEW_PRODUCT = "new_product"
    REMOVED_PRODUCT = "removed_product"
    OUT_OF_STOCK = "out_of_stock"
    BACK_IN_STOCK = "back_in_stock"

    ALL = (
        PRICE_INCREASE,
        PRICE_DECREASE,
        NEW_PRODUCT,
        REMOVED_PRODUCT,
        OUT_OF_STOCK,
        BACK_IN_STOCK,
    )


class ChangeSeverity:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    #: Sort weight, biggest first.
    ORDER = {HIGH: 2, MEDIUM: 1, LOW: 0}


class ReportStatus:
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# --------------------------------------------------------------------------
# Shops — the customer's own storefront
# --------------------------------------------------------------------------
class Shop(Base):
    """One customer storefront that competitors are benchmarked against."""

    __tablename__ = "shops"
    __table_args__ = (
        # Unique per tenant, not globally: two customers can both sell on the
        # same marketplace, and one must not be able to block the other.
        UniqueConstraint("organization_id", "domain", name="uq_shop_org_domain"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    domain: Mapped[str] = mapped_column(String(255), index=True)
    platform: Mapped[str] = mapped_column(String(40), default=Platform.UNKNOWN, index=True)
    #: Free-text seller category, e.g. "homeware", "outdoor", "kitchen".
    category: Mapped[str] = mapped_column(String(120), default="", index=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")

    #: How often to re-crawl. 168 = weekly, the product's headline cadence.
    crawl_frequency_hours: Mapped[int] = mapped_column(Integer, default=168)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    last_crawled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    next_crawl_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_status: Mapped[str] = mapped_column(String(24), default="")
    tracked_products: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    competitors: Mapped[list["Competitor"]] = relationship(
        back_populates="shop", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------
# Competitors
# --------------------------------------------------------------------------
class Competitor(Base):
    """A storefront we watch on the customer's behalf."""

    __tablename__ = "competitors"
    __table_args__ = (
        UniqueConstraint("organization_id", "domain", name="uq_competitor_org_domain"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: NULL means "watched across the organization", not tied to one shop.
    shop_id: Mapped[int | None] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), nullable=True, index=True
    )

    domain: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    platform: Mapped[str] = mapped_column(String(40), default=Platform.UNKNOWN, index=True)
    #: "manual" | "search" | "seed"
    discovery_method: Mapped[str] = mapped_column(String(24), default="manual", index=True)
    #: 0..1. Search-discovered competitors start low; the customer can confirm.
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_crawled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_status: Mapped[str] = mapped_column(String(24), default="")
    #: How the most recent crawl read this competitor: "feed" (its own public
    #: API), "jsonld" (schema.org on the page), or "html" (parsed page). Shown
    #: to the customer, because "exact, from their feed" is a materially
    #: stronger claim than "we read the page and think this is the price".
    last_source: Mapped[str] = mapped_column(String(16), default="", index=True)
    product_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    shop: Mapped["Shop | None"] = relationship(back_populates="competitors")
    products: Mapped[list["CompetitorProduct"]] = relationship(
        back_populates="competitor", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------
# Competitor products — a time series, one row per capture
# --------------------------------------------------------------------------
class CompetitorProduct(Base):
    """One capture of one product at one competitor.

    Rows are never updated: a re-crawl inserts a new one, and change detection
    compares the two most recent captures for the same ``(competitor, url)``.
    That is what makes "who dropped their price this week" answerable, and what
    makes the evidence link behind the claim reproducible.
    """

    __tablename__ = "competitor_products"
    __table_args__ = (
        Index("ix_cp_competitor_url_time", "competitor_id", "source_url", "captured_at"),
        Index("ix_cp_org_time", "organization_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id", ondelete="CASCADE"), index=True
    )
    #: The crawl that produced this capture, for debugging one specific run.
    crawl_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    source_url: Mapped[str] = mapped_column(Text)
    #: Shopify product id / Woo SKU, when the page exposes one.
    external_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    name: Mapped[str] = mapped_column(String(400))
    #: Lowercased, punctuation-stripped. The match key across crawls, and across
    #: competitors when we look for the same product everywhere.
    normalized_name: Mapped[str] = mapped_column(String(400), index=True)
    brand: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(160), default="", index=True)

    #: Money is Decimal end to end: see MONEY in app/core/database/base.py.
    #: A float cannot hold 0.45, and this column is what we tell a
    #: customer a competitor charges.
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    availability: Mapped[str] = mapped_column(String(40), default="unknown", index=True)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    image_url: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")

    #: The "was" price when the shop is running a sale, and the real price when
    #: it is not. `None` means "not on sale" -- which is itself worth recording,
    #: because a discount that disappears is a change customers care about.
    compare_at_price: Mapped[Decimal | None] = mapped_column(
        MONEY, nullable=True, index=True
    )
    #: Platform feed / jsonld / html -- where this row's numbers came from.
    #: "feed" is the shop's own public API (exact and complete), "jsonld" is
    #: schema.org markup on a single page (exact), "html" means we parsed the
    #: rendered page. Shown in the UI so a customer knows which they are reading.
    data_source: Mapped[str] = mapped_column(String(16), default="html", index=True)
    #: Number of purchasable variants, when the source reports one.
    variant_count: Mapped[int] = mapped_column(Integer, default=1)
    #: Barcode (GTIN/EAN) when the shop publishes one. The strongest key for
    #: pairing this product with one in the customer's own catalogue.
    gtin: Mapped[str] = mapped_column(String(32), default="", index=True)
    #: json-ld / opengraph / html — how confidently we read the price.
    extraction_method: Mapped[str] = mapped_column(String(24), default="unknown")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    competitor: Mapped["Competitor"] = relationship(back_populates="products")


# --------------------------------------------------------------------------
# The customer's own catalogue, and which competitor products are the same
# --------------------------------------------------------------------------
class ShopProduct(Base):
    """One capture of one product in the customer's own shop.

    A time series like ``competitor_products``, read the same way (shopfeed),
    so "your price" on a chart is a reading with a date, not a value that
    silently changes under old reports.
    """

    __tablename__ = "shop_products"
    __table_args__ = (
        Index("ix_sp_shop_url_time", "shop_id", "source_url", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True
    )
    source_url: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(String(160), default="")
    name: Mapped[str] = mapped_column(String(400))
    brand: Mapped[str] = mapped_column(String(200), default="")
    gtin: Mapped[str] = mapped_column(String(32), default="")
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    data_source: Mapped[str] = mapped_column(String(16), default="feed")
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class MatchConfidence:
    """How sure we are two products are the same. Shown to the customer.

    Only a shared barcode is ``certain``. A title match is at best ``likely``:
    two shops can word the same candle the same way and still sell different
    ones, so the UI must never present it as fact.
    """

    CERTAIN = "certain"
    LIKELY = "likely"
    POSSIBLE = "possible"

    #: A title score at or above this is "likely"; below it (down to the
    #: matcher's own floor) the pair is only "possible".
    LIKELY_SCORE = 0.9

    @classmethod
    def of(cls, method: str, score: float) -> str:
        if method == "gtin":
            return cls.CERTAIN
        return cls.LIKELY if score >= cls.LIKELY_SCORE else cls.POSSIBLE


class ProductMatch(Base):
    """One of the customer's products paired with one competitor product.

    Keyed by product URL, not capture id: captures are a time series, a match
    is between products. Rebuilt per (shop, competitor) after each crawl.
    """

    __tablename__ = "product_matches"
    __table_args__ = (
        Index("ix_pm_competitor_url", "competitor_id", "competitor_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True
    )
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id", ondelete="CASCADE"), index=True
    )
    own_url: Mapped[str] = mapped_column(Text)
    own_name: Mapped[str] = mapped_column(String(400), default="")
    competitor_url: Mapped[str] = mapped_column(Text)
    competitor_name: Mapped[str] = mapped_column(String(400), default="")
    #: "gtin" (same barcode) or "title" (shopfeed's title rules).
    method: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def confidence(self) -> str:
        return MatchConfidence.of(self.method, self.score)


# --------------------------------------------------------------------------
# Change events — the in-app alert inbox
# --------------------------------------------------------------------------
class ChangeEvent(Base):
    """One detected change, with a link to the evidence that proves it."""

    __tablename__ = "change_events"
    __table_args__ = (
        Index("ix_change_org_detected", "organization_id", "detected_at"),
        # The same observation must never be alerted twice, even if two crawls
        # race. See ecommerce/changes.py for the pre-insert check.
        Index("ix_change_dedupe", "organization_id", "kind", "competitor_id",
              "source_url", "previous_price", "new_price"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    shop_id: Mapped[int | None] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), nullable=True, index=True
    )
    competitor_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitors.id", ondelete="CASCADE"), nullable=True, index=True
    )
    competitor_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitor_products.id", ondelete="CASCADE"), nullable=True
    )

    kind: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), default=ChangeSeverity.LOW, index=True)

    product_name: Mapped[str] = mapped_column(String(400), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    #: The competitor's storefront, so the UI can say "Acme dropped a price".
    competitor_name: Mapped[str] = mapped_column(String(200), default="")
    competitor_domain: Mapped[str] = mapped_column(String(255), default="", index=True)

    previous_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    new_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    delta: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    delta_pct: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)

    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    #: Where a human can go and check the claim themselves.
    evidence_url: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
class Report(Base):
    """A written report over a period of detected changes.

    The AI writes the narrative; the numbers come from the change_events the
    diff engine already computed. Both are stored, so a customer can re-read
    last month's report and the UI can render a table next to the prose.
    """

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    shop_id: Mapped[int | None] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), nullable=True, index=True
    )

    #: "weekly" | "monthly" | "manual"
    kind: Mapped[str] = mapped_column(String(24), default="weekly", index=True)
    status: Mapped[str] = mapped_column(String(24), default=ReportStatus.PENDING, index=True)

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    title: Mapped[str] = mapped_column(String(300), default="")
    markdown: Mapped[str] = mapped_column(Text, default="")
    #: The structured facts the narrative was written from. Never model output.
    facts: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    change_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)

    model: Mapped[str] = mapped_column(String(120), default="")
    #: The language the prose was written in ("nl" | "en"); the facts have none.
    language: Mapped[str] = mapped_column(String(8), default="en", server_default="en")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "ChangeEvent",
    "ChangeKind",
    "ChangeSeverity",
    "Competitor",
    "CompetitorProduct",
    "CrawlStatus",
    "MatchConfidence",
    "Platform",
    "ProductMatch",
    "Report",
    "ReportStatus",
    "Shop",
    "ShopProduct",
]

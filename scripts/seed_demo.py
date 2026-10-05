"""Seed a realistic demo workspace for Sparton Intelligence.

    python scripts/seed_demo.py [--email demo@sparton.io] [--password ...] [--days 21]

Idempotent: re-running tops the workspace back up rather than duplicating it.
Development only — never point this at a production database.

This seeds the *whole product loop* so the dashboard has something real to
show: a shop, three competitors, several weeks of competitor product captures,
the price changes that fall out of comparing them, and a written report.

No network access is required or attempted: the competitor catalogues are
generated here and written straight into the capture time series.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.auth.service import hash_password, unique_slug  # noqa: E402
from app.core.database.base import Base, SessionLocal, engine  # noqa: E402
from app.core.database.ecommerce_models import (  # noqa: E402
    ChangeEvent,
    ChangeKind,
    ChangeSeverity,
    Competitor,
    CompetitorProduct,
    CrawlStatus,
    Platform,
    Report,
    ReportStatus,
    Shop,
)
from app.core.database.identity import Organization, User  # noqa: E402
from app.core.database.models import utcnow  # noqa: E402
from app.ecommerce.changes import normalize_name  # noqa: E402

#: A believable small homeware seller, and the rivals it competes with.
DEMO_SHOP = {
    "name": "Acme Homeware",
    "domain": "acme-homeware.myshopify.com",
    "category": "homeware",
    "currency": "EUR",
}

DEMO_COMPETITORS = [
    ("Claybarn Co", "claybarn-co.myshopify.com", "ceramics"),
    ("Northlight Goods", "northlight-goods.myshopify.com", "lighting"),
    ("Fold Textiles", "fold-textiles.myshopify.com", "textiles"),
]

#: (name, starting price). The seed perturbs these over the simulated weeks.
CATALOGUE = [
    ("Matte Ceramic Mug", 24.00),
    ("Linen Table Runner", 34.00),
    ("Brass Desk Lamp", 89.00),
    ("Stoneware Bowl Set", 62.00),
    ("Waffle Cotton Throw", 48.00),
    ("Cast Iron Skillet", 54.00),
    ("Terracotta Planter", 28.00),
    ("Oak Cutting Board", 42.00),
    ("Ribbed Glass Vase", 31.00),
    ("Wool Floor Cushion", 96.00),
    ("Copper Watering Can", 38.00),
    ("Speckled Dinner Plate", 19.00),
]


def _slug_handle(name: str) -> str:
    return name.lower().replace(" ", "-")


def seed(email: str, password: str, org_name: str, weeks: int = 3) -> None:
    """Create (or top up) a demo workspace showing the whole product loop."""
    Base.metadata.create_all(bind=engine)
    # Fixed seed: re-running produces the same demo, which is what makes this
    # usable as a screenshot source and as a manual test fixture.
    rng = random.Random(7)
    db = SessionLocal()
    try:
        org = db.execute(
            select(Organization).where(Organization.name == org_name)
        ).scalars().first()
        if org is None:
            org = Organization(name=org_name, slug=unique_slug(db, org_name))
            db.add(org)
            db.flush()
            print(f"  workspace  {org_name}")

        user = db.execute(select(User).where(User.email == email)).scalars().first()
        if user is None:
            user = User(
                organization_id=org.id,
                email=email,
                password_hash=hash_password(password),
                role="admin",
            )
            db.add(user)
            db.flush()
            print(f"  user       {email} / {password}")
        else:
            print(f"  user       {email} (existing)")

        # --- the customer's own shop -----------------------------------
        shop = db.execute(
            select(Shop).where(
                Shop.organization_id == org.id, Shop.domain == DEMO_SHOP["domain"]
            )
        ).scalars().first()
        if shop is None:
            shop = Shop(
                organization_id=org.id,
                name=DEMO_SHOP["name"],
                url=f"https://{DEMO_SHOP['domain']}",
                domain=DEMO_SHOP["domain"],
                platform=Platform.SHOPIFY,
                category=DEMO_SHOP["category"],
                currency=DEMO_SHOP["currency"],
                crawl_frequency_hours=168,
                is_active=True,
            )
            db.add(shop)
            db.flush()
            print(f"  shop       {shop.name} ({shop.domain})")

        # --- competitors -------------------------------------------------
        for name, domain, niche in DEMO_COMPETITORS:
            existing = db.execute(
                select(Competitor).where(
                    Competitor.organization_id == org.id, Competitor.domain == domain
                )
            ).scalars().first()
            if existing is None:
                db.add(Competitor(
                    organization_id=org.id, shop_id=shop.id,
                    name=name, domain=domain, url=f"https://{domain}",
                    platform=Platform.SHOPIFY, discovery_method="manual",
                    confidence=1.0, is_active=True, product_count=len(CATALOGUE),
                ))
                db.flush()
        competitors = db.execute(
            select(Competitor).where(Competitor.organization_id == org.id)
        ).scalars().all()
        print(f"  competitors {len(competitors)}")

        # --- weeks of captures, and the changes between them --------------
        added = _seed_history(db, org.id, shop, competitors, rng, weeks)
        print(f"  history    {added['captures']} captures across {weeks} weeks")
        print(f"  changes    {added['price']} price moves, {added['assortment']} assortment")
        if added["report"]:
            print(f"  report     {added['report']}")

        db.commit()
        db.commit()
    finally:
        db.close()


def _seed_history(db, org_id: int, shop: Shop, competitors, rng, weeks: int) -> dict:
    """Simulate `weeks` crawls and derive the changes between them.

    Rather than faking change rows, this writes a real capture time series and
    runs it through the real diff engine. The demo data is therefore exactly
    what the product would have produced, which is the point: a demo that does
    not match real output is worse than no demo.
    """
    from app.ecommerce.changes import diff_captures
    from app.ecommerce.reports import generate_report

    existing = db.execute(
        select(CompetitorProduct).where(CompetitorProduct.organization_id == org_id)
    ).scalars().first()
    if existing is not None:
        print("  history    already seeded (delete the database to re-seed)")
        return {"captures": 0, "price": 0, "assortment": 0, "report": ""}

    now = utcnow()
    base = dict(CATALOGUE)
    live: dict[int, dict[str, float]] = {c.id: dict(base) for c in competitors}
    delisted: dict[int, set[str]] = {c.id: set() for c in competitors}
    captures = 0

    for week in range(weeks, 0, -1):
        moment = now - timedelta(days=7 * week)

        # Move the market *before* writing this week's captures, so week 1
        # establishes the baseline and every later week has something to diff
        # against. Perturbing afterwards would make week N identical to N+1.
        if week < weeks:
            for competitor in competitors:
                live_names = [n for n in base if n not in delisted[competitor.id]]
                for _ in range(rng.randint(1, 3)):
                    name = rng.choice(live_names)
                    move = rng.choice([-0.22, -0.15, -0.08, 0.06, 0.12, 0.25])
                    live[competitor.id][name] = round(
                        live[competitor.id][name] * (1 + move), 2
                    )
                # The last competitor quietly drops a listing, now and then.
                if competitor is competitors[-1] and rng.random() < 0.6 and live_names:
                    delisted[competitor.id].add(rng.choice(live_names))

        for competitor in competitors:
            this_week: dict[str, CompetitorProduct] = {}
            for name in base:
                if name in delisted[competitor.id]:
                    continue
                handle = _slug_handle(name)
                row = CompetitorProduct(
                    organization_id=org_id,
                    competitor_id=competitor.id,
                    source_url=f"https://{competitor.domain}/products/{handle}",
                    external_id=handle.upper(),
                    name=name,
                    normalized_name=normalize_name(name),
                    category="Homeware",
                    price=Decimal(str(round(live[competitor.id][name], 2))),
                    currency="EUR",
                    availability="in_stock",
                    in_stock=True,
                    image_url=f"https://{competitor.domain}/cdn/{handle}.jpg",
                    extraction_method="json-ld",
                    confidence=0.95,
                    captured_at=moment,
                )
                db.add(row)
                db.flush()
                this_week[name] = row
                captures += 1

            # Run the previous crawl's rows through the production diff engine.
            last_week = db.execute(
                select(CompetitorProduct)
                .where(
                    CompetitorProduct.competitor_id == competitor.id,
                    CompetitorProduct.captured_at < moment,
                )
                .order_by(CompetitorProduct.captured_at.desc())
            ).scalars().all()
            before = {r.normalized_name: r for r in last_week}
            for row in this_week.values():
                prior = before.get(row.normalized_name)
                if prior is None:
                    continue
                for change in diff_captures(prior, row, competitor=competitor):
                    db.add(_change_row(org_id, shop.id, competitor, change, moment))
        db.flush()

    db.commit()

    counts = _change_counts(db, org_id, shop.id)
    report = generate_report(
        db, organization_id=org_id, shop=shop, competitors=list(competitors),
        days=weeks * 7 + 1, kind="weekly", use_llm=False,
    )
    for competitor in competitors:
        competitor.last_status = CrawlStatus.COMPLETED
        competitor.last_crawled_at = now
    shop.last_crawled_at = now
    shop.last_status = CrawlStatus.COMPLETED
    shop.tracked_products = len(CATALOGUE)
    db.commit()

    return {
        "captures": captures,
        "price": counts["price"],
        "assortment": counts["assortment"],
        "report": report.title,
    }


def _change_row(org_id, shop_id, competitor, change, detected_at) -> ChangeEvent:
    return ChangeEvent(
        organization_id=org_id,
        shop_id=shop_id,
        competitor_id=competitor.id,
        competitor_product_id=change.competitor_product_id,
        kind=change.kind,
        severity=change.severity,
        product_name=change.product_name,
        source_url=change.source_url,
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
        detected_at=detected_at,
    )


def _change_counts(db, org_id: int, shop_id: int) -> dict[str, int]:
    from sqlalchemy import func

    rows = db.execute(
        select(ChangeEvent.kind, func.count(ChangeEvent.id))
        .where(ChangeEvent.organization_id == org_id, ChangeEvent.shop_id == shop_id)
        .group_by(ChangeEvent.kind)
    ).all()
    kinds = dict(rows)
    return {
        "price": (
            kinds.get(ChangeKind.PRICE_DECREASE, 0) + kinds.get(ChangeKind.PRICE_INCREASE, 0)
        ),
        "assortment": (
            kinds.get(ChangeKind.NEW_PRODUCT, 0) + kinds.get(ChangeKind.REMOVED_PRODUCT, 0)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a Sparton Intelligence demo")
    parser.add_argument("--email", default="demo@sparton.io")
    parser.add_argument("--password", default="sparton-demo-1")
    parser.add_argument("--org", default="Demo Workspace")
    parser.add_argument("--weeks", type=int, default=3, help="weeks of history to simulate")
    args = parser.parse_args()

    print("Seeding a Sparton Intelligence demo workspace…")
    seed(args.email, args.password, args.org, weeks=max(1, args.weeks))
    print()
    print("Sign in at http://127.0.0.1:8000/app/")
    print(f"  {args.email} / {args.password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

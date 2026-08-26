"""Seed a demo workspace so the dashboard can be exercised with real data.

    python scripts/seed_demo.py [--email demo@sparton.io] [--password ...]

Idempotent: re-running tops the workspace back up rather than duplicating it.
Development only — never point this at a production database.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.auth.service import hash_password  # noqa: E402
from app.core.database.base import Base, SessionLocal, engine  # noqa: E402
from app.core.database.creation_models import Document  # noqa: E402
from app.core.database.domain_models import (  # noqa: E402
    AgentRun,
    AgentStep,
    ApprovalRequest,
    ExternalProduct,
    ExternalStore,
    Inventory,
    Job,
    Opportunity,
    Order,
    Product,
)
from app.core.database.identity import Organization, User  # noqa: E402
from app.core.database.models import utcnow  # noqa: E402

CATEGORIES = ["Homeware", "Lighting", "Textiles", "Kitchen", "Outdoor"]
TITLES = [
    "Matte Ceramic Mug", "Linen Table Runner", "Brass Desk Lamp", "Stoneware Bowl Set",
    "Waffle Cotton Throw", "Cast Iron Skillet", "Terracotta Planter", "Oak Cutting Board",
    "Ribbed Glass Vase", "Wool Floor Cushion", "Copper Watering Can", "Speckled Dinner Plate",
]


def seed(email: str, password: str, org_name: str) -> None:
    Base.metadata.create_all(bind=engine)
    rng = random.Random(7)  # deterministic demo data
    db = SessionLocal()
    try:
        org = db.execute(select(Organization).where(Organization.name == org_name)).scalars().first()
        if org is None:
            org = Organization(name=org_name)
            db.add(org)
            db.flush()

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
            print(f"  user      {email} / {password}")
        else:
            print(f"  user      {email} (existing)")

        # --- catalog ------------------------------------------------------
        existing = db.execute(
            select(Product).where(Product.organization_id == org.id)
        ).scalars().all()
        if not existing:
            for index, name in enumerate(TITLES):
                quantity = rng.choice([0, 3, 8, 24, 60, 140])
                product = Product(
                    organization_id=org.id,
                    sku=f"SP-{1000 + index}",
                    title=name,
                    description="" if index % 4 == 0 else f"{name} in a small-batch finish.",
                    category=CATEGORIES[index % len(CATEGORIES)],
                    price=round(rng.uniform(12, 180), 2),
                    status="active" if index % 5 else "draft",
                )
                db.add(product)
                db.flush()
                db.add(Inventory(product_id=product.id, quantity=quantity, reorder_point=10))
                for _ in range(rng.randint(0, 6)):
                    db.add(Order(
                        product_id=product.id,
                        quantity=rng.randint(1, 3),
                        total=round(product.price * rng.randint(1, 3), 2),
                        created_at=utcnow() - timedelta(days=rng.randint(0, 60)),
                    ))
            print(f"  catalog   {len(TITLES)} products with inventory and orders")
        else:
            print(f"  catalog   {len(existing)} products (existing)")

        # --- agent runs + a pending approval ------------------------------
        if not db.execute(select(AgentRun).where(AgentRun.organization_id == org.id)).scalars().first():
            specs = [
                ("Audit product descriptions and flag the weak ones", "COMPLETED", 4),
                ("Restock anything below its reorder point", "AWAITING_APPROVAL", 3),
                ("Summarise last month's orders by category", "COMPLETED", 2),
                ("Reprice the outdoor range against competitors", "FAILED", 2),
            ]
            for request, status, steps in specs:
                run = AgentRun(
                    organization_id=org.id,
                    user_request=request,
                    status=status,
                    iterations=steps,
                    tool_calls_made=steps,
                    started_at=utcnow() - timedelta(hours=rng.randint(1, 70)),
                    completed_at=None if status == "AWAITING_APPROVAL" else utcnow(),
                    final_response=(
                        "Reviewed the catalog and produced the requested summary."
                        if status == "COMPLETED" else None
                    ),
                    error="Competitor endpoint timed out after 3 retries." if status == "FAILED" else None,
                )
                db.add(run)
                db.flush()
                for number in range(1, steps + 1):
                    db.add(AgentStep(
                        agent_run_id=run.id,
                        step_number=number,
                        step_type="tool_call" if number % 2 else "thought",
                        message=f"Step {number} of “{request[:40]}…”",
                        tool_name="search_products" if number % 2 else None,
                        status="COMPLETED" if status != "FAILED" or number < steps else "FAILED",
                    ))
                if status == "AWAITING_APPROVAL":
                    db.add(ApprovalRequest(
                        organization_id=org.id,
                        agent_run_id=run.id,
                        tool_name="update_inventory",
                        summary="Raise stock on 3 low SKUs to 40 units each",
                        preview={"changes": [
                            {"field": "SP-1003.quantity", "current": 3, "proposed": 40},
                            {"field": "SP-1007.quantity", "current": 0, "proposed": 40},
                        ]},
                        status="PENDING",
                    ))
            print("  agent     4 runs, 1 pending approval")

        # --- research -----------------------------------------------------
        if not db.execute(select(Job).where(Job.organization_id == org.id)).scalars().first():
            db.add(Job(
                organization_id=org.id, type="research", status="COMPLETED", stage="scoring",
                payload={"query": "minimalist ceramic homeware"},
                stats={"domains_discovered": 14, "pages_crawled": 212,
                       "products_discovered": 486, "opportunities_found": 5},
            ))
            for index, (domain, niche) in enumerate([
                ("claybarn.example", "ceramics"), ("northlight.example", "lighting"),
                ("fold-textiles.example", "textiles"),
            ]):
                store = ExternalStore(
                    organization_id=org.id, domain=domain, name=domain.split(".")[0].title(),
                    niche=niche, platform="shopify", crawl_status="COMPLETED",
                    last_crawled_at=utcnow() - timedelta(days=index),
                )
                db.add(store)
                db.flush()
                for n in range(rng.randint(4, 9)):
                    name = f"{store.name} item {n}"
                    db.add(ExternalProduct(
                        store_id=store.id, source_url=f"https://{domain}/p/{n}",
                        name=name, normalized_name=name.lower(), category=niche,
                        price=round(rng.uniform(15, 140), 2), currency="USD",
                        availability="in_stock", confidence=0.8,
                    ))

            for kind, name, summary, score, competition in [
                ("product_gap", "No mid-price stoneware dinner sets",
                 "Competitors sell either budget or premium sets; the $60–90 band is empty.", 82.0, "low"),
                ("pricing", "Brass lighting is priced 22% below market",
                 "Three competitors price comparable brass lamps materially higher.", 74.5, "medium"),
                ("niche", "Outdoor textiles show rising assortment growth",
                 "Assortment in outdoor textiles grew across every crawled store.", 61.0, "high"),
            ]:
                db.add(Opportunity(
                    organization_id=org.id, type=kind, title=name, summary=summary,
                    recommended_action="Prototype three SKUs and test against the existing range.",
                    score=score, confidence=round(rng.uniform(0.55, 0.92), 2),
                    competition_level=competition, status="NEW",
                    source_urls=[f"https://claybarn.example/collections/{kind}"],
                    evidence={"stores_observed": 3, "products_observed": 486},
                ))
            print("  research  1 job, 3 stores, 3 opportunities")

        # --- documents ----------------------------------------------------
        if not db.execute(select(Document).where(Document.organization_id == org.id)).scalars().first():
            for name, size, chunks in [
                ("Brand voice guidelines.pdf", 184_320, 24),
                ("2024 supplier contracts.docx", 96_100, 12),
                ("Product photography brief.md", 8_400, 3),
            ]:
                db.add(Document(
                    organization_id=org.id, filename=name, title=name,
                    content_hash=f"{abs(hash(name)):064x}"[:64],
                    size_bytes=size, chunk_count=chunks, created_by=user.id,
                ))
            print("  knowledge 3 documents")

        db.commit()
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed SPARTON demo data")
    parser.add_argument("--email", default="demo@sparton.io")
    parser.add_argument("--password", default="sparton-demo-1")
    parser.add_argument("--org", default="Demo Workspace")
    args = parser.parse_args()

    print("Seeding SPARTON demo workspace…")
    seed(args.email, args.password, args.org)
    print("Done. Sign in at http://127.0.0.1:8000/dashboard/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

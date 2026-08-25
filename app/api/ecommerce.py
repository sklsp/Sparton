"""E-commerce domain API: catalog, inventory, analytics."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from app.core.auth.api import DbSession, current_user
from app.core.database.domain_models import Inventory, Order, Product
from app.core.database.models import utcnow

router = APIRouter(tags=["ecommerce"])


def _org_id(user) -> int | None:
    return getattr(user, "organization_id", None)


def _product_dict(p: Product) -> dict:
    return {
        "id": p.id, "sku": p.sku, "title": p.title,
        "description": p.description, "price": p.price,
        "category": p.category, "status": p.status,
        "inventory": p.inventory_quantity,
    }


@router.get("/products")
def list_products(
    search: str | None = None,
    limit: int = 50,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    query = select(Product).order_by(Product.id).limit(min(limit, 200))
    org = _org_id(user)
    if org is not None:
        query = query.where(Product.organization_id == org)
    if search:
        query = query.where(Product.title.ilike(f"%{search}%"))
    rows = db.execute(query).scalars().all()
    return {"count": len(rows), "products": [_product_dict(r) for r in rows]}


@router.get("/products/{product_id}")
def get_product(product_id: int, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    row = db.get(Product, product_id)
    org = _org_id(user)
    if row is None or (org is not None and row.organization_id != org):
        raise HTTPException(status_code=404, detail="Product not found")
    return _product_dict(row)


@router.get("/analytics/summary")
def analytics_summary(db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    org = _org_id(user)

    product_query = select(func.count(Product.id))
    order_query = select(func.count(Order.id), func.coalesce(func.sum(Order.total), 0.0))
    low_stock_query = (
        select(func.count(Inventory.id))
        .join(Product, Inventory.product_id == Product.id)
        .where(Inventory.quantity <= Inventory.reorder_point)
    )
    if org is not None:
        product_query = product_query.where(Product.organization_id == org)
        order_query = order_query.where(Product.organization_id == org).join(Product, Order.product_id == Product.id)
        low_stock_query = low_stock_query.where(Product.organization_id == org)

    product_count = db.execute(product_query).scalar() or 0
    order_count, total_revenue = db.execute(order_query).one()
    low_stock = db.execute(low_stock_query).scalar() or 0

    since = utcnow() - timedelta(days=30)
    recent_orders = db.execute(
        select(func.count(Order.id)).where(Order.created_at >= since)
    ).scalar() or 0

    return {
        "catalog": {"product_count": product_count},
        "inventory": {"low_stock_count": low_stock},
        "sales": {
            "order_count_total": order_count,
            "total_revenue": round(float(total_revenue), 2),
            "orders_last_30_days": recent_orders,
        },
    }


__all__ = ["router"]

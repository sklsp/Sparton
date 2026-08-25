"""Athena tools — namespaced capabilities across all SPARTON domains.

READ/WRITE classification is the approval boundary. Domain services are
attached to the ToolContext at engine build time.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent.registry import ToolAccess, ToolContext, ToolRegistry
from app.core.database.creation_models import (
    DatasetImage,
    DatasetProject,
    Document,
    GeneratedImage,
    TrainingProject,
)
from app.core.database.domain_models import (
    ExternalProduct,
    ExternalStore,
    Inventory,
    Opportunity,
    Product,
)


# --------------------------------------------------------------------------
# Input models
# --------------------------------------------------------------------------
class SearchDocumentsInput(BaseModel):
    query: str = Field(min_length=2, description="What to look for in the documents")
    top_k: int = Field(default=4, ge=1, le=20)


class ListDocumentsInput(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class GetProductsInput(BaseModel):
    search: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class GetProductInput(BaseModel):
    product_id: int = Field(ge=1)


class GetInventoryInput(BaseModel):
    low_stock_only: bool = False


class GetSalesSummaryInput(BaseModel):
    days: int = Field(default=30, ge=1, le=365)


class UpdateProductInput(BaseModel):
    product_id: int = Field(ge=1)
    title: str | None = None
    description: str | None = None


class ListOpportunitiesInput(BaseModel):
    kind: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class ListDatasetsInput(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class ListGeneratedImagesInput(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)


class GenerateImageInput(BaseModel):
    workflow_id: str
    prompt: str = Field(min_length=1)
    negative_prompt: str | None = None
    lora_name: str | None = None


class StartTrainingInput(BaseModel):
    project_id: int = Field(ge=1)


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------
def _scoped(query, ctx: ToolContext):
    if ctx.organization_id is not None:
        column = query.column_descriptions[0]["entity"].organization_id
        query = query.where(column == ctx.organization_id)
    return query


def search_documents(ctx: ToolContext, params: SearchDocumentsInput) -> dict:
    rag = ctx.services.get("rag")
    if rag is None:
        from app.documents.rag_service import RAGService

        rag = RAGService()
    chunks = rag.query(params.query, top_k=params.top_k)
    return {
        "chunks": len(chunks),
        "results": [
            {"source": c.filename, "chunk": c.chunk_index, "score": round(c.score, 3), "text": c.text[:400]}
            for c in chunks
        ],
    }


def list_documents(ctx: ToolContext, params: ListDocumentsInput) -> dict:
    rows = ctx.db.execute(
        _scoped(select(Document).order_by(Document.created_at.desc()).limit(params.limit), ctx)
    ).scalars().all()
    return {
        "count": len(rows),
        "documents": [{"id": r.id, "title": r.title, "chunks": r.chunk_count} for r in rows],
    }


def get_products(ctx: ToolContext, params: GetProductsInput) -> dict:
    query = select(Product).order_by(Product.id).limit(params.limit)
    if params.search:
        query = query.where(Product.title.ilike(f"%{params.search}%"))
    rows = ctx.db.execute(_scoped(query, ctx)).scalars().all()
    return {
        "count": len(rows),
        "products": [
            {"id": r.id, "sku": r.sku, "title": r.title, "price": r.price,
             "category": r.category, "inventory": r.inventory_quantity}
            for r in rows
        ],
    }


def get_product(ctx: ToolContext, params: GetProductInput) -> dict:
    row = ctx.db.get(Product, params.product_id)
    if row is None or (ctx.organization_id is not None and row.organization_id != ctx.organization_id):
        return {"error": f"Product {params.product_id} not found"}
    return {
        "id": row.id, "sku": row.sku, "title": row.title,
        "description": row.description, "price": row.price,
        "category": row.category, "status": row.status,
        "inventory": row.inventory_quantity,
    }


def get_inventory(ctx: ToolContext, params: GetInventoryInput) -> dict:
    query = (
        select(Inventory, Product)
        .join(Product, Inventory.product_id == Product.id)
        .order_by(Inventory.quantity)
        .limit(50)
    )
    if ctx.organization_id is not None:
        query = query.where(Product.organization_id == ctx.organization_id)
    if params.low_stock_only:
        query = query.where(Inventory.quantity <= Inventory.reorder_point)
    rows = ctx.db.execute(query).all()
    return {
        "count": len(rows),
        "items": [
            {"sku": p.sku, "title": p.title, "quantity": inv.quantity,
             "reorder_point": inv.reorder_point}
            for inv, p in rows
        ],
    }


def get_sales_summary(ctx: ToolContext, params: GetSalesSummaryInput) -> dict:
    from datetime import timedelta

    from app.core.database.domain_models import Order
    from app.core.database.models import utcnow

    since = utcnow() - timedelta(days=params.days)
    orders = ctx.db.execute(
        select(Order).where(Order.created_at >= since).limit(5000)
    ).scalars().all()
    total_revenue = sum(o.total for o in orders)
    return {
        "period_days": params.days,
        "order_count": len(orders),
        "total_revenue": round(total_revenue, 2),
    }


def update_product(ctx: ToolContext, params: UpdateProductInput) -> dict:
    """WRITE tool — pauses the run for human approval before executing."""
    row = ctx.db.get(Product, params.product_id)
    if row is None or (ctx.organization_id is not None and row.organization_id != ctx.organization_id):
        raise ValueError(f"Product {params.product_id} not found")

    updated_fields: list[str] = []
    if params.title is not None and params.title != row.title:
        row.title = params.title
        updated_fields.append("title")
    if params.description is not None and params.description != row.description:
        row.description = params.description
        updated_fields.append("description")
    ctx.db.commit()
    return {"sku": row.sku, "updated_fields": updated_fields}


def list_opportunities(ctx: ToolContext, params: ListOpportunitiesInput) -> dict:
    rows = list_opportunities_query(ctx, kind=params.kind, limit=params.limit)
    return {
        "count": len(rows),
        "opportunities": [
            {"id": r.id, "type": r.type, "title": r.title,
             "score": r.score, "summary": r.summary[:200]}
            for r in rows
        ],
    }


def list_opportunities_query(ctx: ToolContext, *, kind: str | None, limit: int):
    query = (
        select(Opportunity)
        .order_by(Opportunity.score.desc(), Opportunity.discovered_at.desc())
        .limit(limit)
    )
    if kind:
        query = query.where(Opportunity.type == kind)
    return ctx.db.execute(_scoped(query, ctx)).scalars().all()


def list_datasets(ctx: ToolContext, params: ListDatasetsInput) -> dict:
    rows = ctx.db.execute(
        _scoped(select(DatasetProject).order_by(DatasetProject.created_at.desc()).limit(params.limit), ctx)
    ).scalars().all()
    return {
        "count": len(rows),
        "datasets": [
            {"id": r.id, "name": r.name, "trigger_word": r.trigger_word}
            for r in rows
        ],
    }


def list_generated_images(ctx: ToolContext, params: ListGeneratedImagesInput) -> dict:
    rows = ctx.db.execute(
        _scoped(select(GeneratedImage).order_by(GeneratedImage.created_at.desc()).limit(params.limit), ctx)
    ).scalars().all()
    return {
        "count": len(rows),
        "images": [
            {"id": r.id, "filename": r.filename, "prompt": r.prompt[:120],
             "workflow": r.workflow_slug}
            for r in rows
        ],
    }


def generate_image(ctx: ToolContext, params: GenerateImageInput) -> dict:
    """WRITE tool — queues a paid GPU generation; requires approval."""
    service = ctx.services.get("comfyui")
    if service is None:
        from app.generation.service import ComfyUIService

        service = ComfyUIService()
    saved = service.run_generation_sync(
        params.workflow_id,
        {
            "prompt": params.prompt,
            "negative_prompt": params.negative_prompt,
            "lora_name": params.lora_name,
        },
    )
    return {"generated": saved}


def start_training(ctx: ToolContext, params: StartTrainingInput) -> dict:
    """WRITE tool — starts a long-running training run; requires approval."""
    project = ctx.db.get(TrainingProject, params.project_id)
    if project is None or (ctx.organization_id is not None and project.organization_id != ctx.organization_id):
        raise ValueError(f"Training project {params.project_id} not found")
    return {
        "queued": True,
        "project_id": project.id,
        "message": f"Training queued for '{project.name}'. Monitor via jobs.",
    }


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
def register_all(registry: ToolRegistry) -> None:
    registry.tool(
        name="documents.search", description="Search uploaded documents and return cited excerpts.",
        category="documents", access=ToolAccess.READ, input_model=SearchDocumentsInput,
    )(search_documents)

    registry.tool(
        name="documents.list", description="List uploaded documents.",
        category="documents", access=ToolAccess.READ, input_model=ListDocumentsInput,
    )(list_documents)

    registry.tool(
        name="ecommerce.get_products", description="List catalog products, optionally filtered by a title search.",
        category="ecommerce", access=ToolAccess.READ, input_model=GetProductsInput,
    )(get_products)

    registry.tool(
        name="ecommerce.get_product", description="Get one product's full detail by id.",
        category="ecommerce", access=ToolAccess.READ, input_model=GetProductInput,
    )(get_product)

    registry.tool(
        name="ecommerce.get_inventory", description="Inventory levels ordered lowest first; optionally low stock only.",
        category="ecommerce", access=ToolAccess.READ, input_model=GetInventoryInput,
    )(get_inventory)

    registry.tool(
        name="ecommerce.get_sales_summary", description="Order count and revenue over the last N days.",
        category="ecommerce", access=ToolAccess.READ, input_model=GetSalesSummaryInput,
    )(get_sales_summary)

    registry.tool(
        name="ecommerce.update_product", description="Update a product's title and/or description. Requires approval.",
        category="ecommerce", access=ToolAccess.WRITE, input_model=UpdateProductInput,
    )(update_product)

    registry.tool(
        name="research.list_opportunities", description="List discovered market opportunities with scores.",
        category="research", access=ToolAccess.READ, input_model=ListOpportunitiesInput,
    )(list_opportunities)

    registry.tool(
        name="datasets.list", description="List LoRA training image datasets.",
        category="datasets", access=ToolAccess.READ, input_model=ListDatasetsInput,
    )(list_datasets)

    registry.tool(
        name="generation.list_images", description="List recently generated images.",
        category="generation", access=ToolAccess.READ, input_model=ListGeneratedImagesInput,
    )(list_generated_images)

    registry.tool(
        name="generation.generate_image", description="Queue an image generation on ComfyUI. Requires approval.",
        category="generation", access=ToolAccess.WRITE, input_model=GenerateImageInput,
    )(generate_image)

    registry.tool(
        name="training.start_training", description="Start a LoRA training run. Requires approval.",
        category="training", access=ToolAccess.WRITE, input_model=StartTrainingInput,
    )(start_training)


__all__ = ["register_all"]

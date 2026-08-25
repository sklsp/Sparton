"""Domain models: agent audit trail, durable jobs, store, market intelligence.

Adapted from Ares `db/models.py`. Every tenant-owned object carries
`organization_id`; cross-tenant access is impossible through ID manipulation
(scoped queries + generic 404s).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base, JSONType
from app.core.database.models import as_utc, utcnow


class RunStatus:
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @classmethod
    def terminal(cls) -> set[str]:
        return {cls.COMPLETED, cls.FAILED}


class ApprovalStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class StepType:
    REQUEST = "request"
    DECISION = "decision"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESOLVED = "approval_resolved"
    FINAL = "final"
    ERROR = "error"


class StepStatus:
    OK = "ok"
    ERROR = "error"
    PENDING = "pending"


# --------------------------------------------------------------------------
# Agent audit trail
# --------------------------------------------------------------------------
class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Tenant scope: derived server-side from authenticated identity, never
    # trusted from client input.
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), index=True, default="default")
    user_request: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.RUNNING, index=True)
    final_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Serialized conversation, so a run paused for approval can be resumed by
    # any worker (or after a restart) instead of living in process memory.
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    # Tool calls from the current batch that have not been processed yet.
    pending_tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls_made: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AgentStep.step_number",
    )
    approvals: Mapped[list["ApprovalRequest"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ApprovalRequest.id",
    )

    @property
    def duration_ms(self) -> int | None:
        if self.completed_at is None:
            return None
        delta = as_utc(self.completed_at) - as_utc(self.started_at)
        return int(delta.total_seconds() * 1000)


class AgentStep(Base):
    """One observable event in a run. This is what the activity feed renders."""

    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    step_number: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    output: Mapped[Any | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=StepStatus.OK)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[AgentRun] = relationship(back_populates="steps")


Index("ix_agent_steps_run_number", AgentStep.agent_run_id, AgentStep.step_number)


class ApprovalRequest(Base):
    """A write tool call the agent wants to perform, held until a human decides."""

    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # Human readable before/after diff rendered by the approvals UI.
    preview: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.PENDING, index=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[Any | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[AgentRun] = relationship(back_populates="approvals")


# --------------------------------------------------------------------------
# Durable job platform (shared by every domain's long-running work)
# --------------------------------------------------------------------------
class JobStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Job(Base):
    """One unit of long-running work. The DB row is the source of truth;
    the queue transport is only the execution mechanism."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(24), default=JobStatus.QUEUED, index=True)
    stage: Mapped[str] = mapped_column(String(120), default="queued")
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    outputs: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False)

    # Durable queue fields (multi-process workers).
    priority: Mapped[int] = mapped_column(Integer, default=5, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------
# Store (first-party catalog the agent operates on)
# --------------------------------------------------------------------------
class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    inventory: Mapped["Inventory | None"] = relationship(
        back_populates="product", uselist=False, cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    @property
    def inventory_quantity(self) -> int:
        return self.inventory.quantity if self.inventory else 0


class Inventory(Base):
    """Stock levels, kept out of the catalog table so reorder policy can
    evolve without migrating products."""

    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), unique=True, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    reorder_point: Mapped[int] = mapped_column(Integer, default=10)
    warehouse: Mapped[str] = mapped_column(String(40), default="MAIN")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    product: Mapped[Product] = relationship(back_populates="inventory")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    total: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    product: Mapped[Product] = relationship(back_populates="orders")


# --------------------------------------------------------------------------
# Market intelligence (external stores + opportunities)
# --------------------------------------------------------------------------
class ExternalStore(Base):
    __tablename__ = "external_stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    domain: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    niche: Mapped[str] = mapped_column(String(160), default="")
    platform: Mapped[str] = mapped_column(String(40), default="unknown")
    country: Mapped[str] = mapped_column(String(8), default="")
    product_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    store_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crawl_status: Mapped[str] = mapped_column(String(24), default="discovered")

    products: Mapped[list["ExternalProduct"]] = relationship(
        back_populates="store", cascade="all, delete-orphan"
    )


class ExternalProduct(Base):
    __tablename__ = "external_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("external_stores.id", ondelete="CASCADE"), index=True
    )
    source_url: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    brand: Mapped[str] = mapped_column(String(160), default="")
    category: Mapped[str] = mapped_column(String(160), default="")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="")
    availability: Mapped[str] = mapped_column(String(40), default="unknown")
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(Text, default="")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    store: Mapped[ExternalStore] = relationship(back_populates="products")
    snapshots: Mapped[list["ProductSnapshot"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductSnapshot(Base):
    __tablename__ = "product_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_product_id: Mapped[int] = mapped_column(
        ForeignKey("external_products.id", ondelete="CASCADE"), index=True
    )
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    availability: Mapped[str] = mapped_column(String(40), default="unknown")
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    product: Mapped[ExternalProduct] = relationship(back_populates="snapshots")


class OpportunityStatus:
    NEW = "NEW"
    REVIEWED = "REVIEWED"
    DISMISSED = "DISMISSED"
    ACTIONED = "ACTIONED"


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text, default="")
    recommended_action: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    source_urls: Mapped[list[str]] = mapped_column(JSONType, default=list)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    competition_level: Mapped[str] = mapped_column(String(24), default="unknown")
    demand_signals: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(24), default=OpportunityStatus.NEW, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OpportunityEvidence(Base):
    __tablename__ = "opportunity_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True
    )
    source_url: Mapped[str] = mapped_column(Text)
    source_domain: Mapped[str] = mapped_column(String(255), index=True)
    claim: Mapped[str] = mapped_column(Text)
    extraction_method: Mapped[str] = mapped_column(String(80), default="structured")
    observed_value: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


__all__ = [
    "AgentRun",
    "AgentStep",
    "ApprovalRequest",
    "ApprovalStatus",
    "ExternalProduct",
    "ExternalStore",
    "Inventory",
    "Job",
    "JobStatus",
    "Opportunity",
    "OpportunityEvidence",
    "OpportunityStatus",
    "Order",
    "Product",
    "ProductSnapshot",
    "RunStatus",
    "StepStatus",
    "StepType",
    "as_utc",
    "utcnow",
]

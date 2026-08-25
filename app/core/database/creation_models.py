"""Domain models for the Knowledge and Create domains.

Adapted from Apollo's filesystem-backed stores into tenant-scoped SQL rows,
while large binary artifacts (document files, dataset images, generated
images, FAISS indexes) remain on disk under ``DATA_DIR`` with DB rows
holding metadata + provenance.
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
from app.core.database.models import utcnow


# --------------------------------------------------------------------------
# documents (Hector) — Knowledge domain
# --------------------------------------------------------------------------
class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(255), default="")
    # sha256 of content — drives incremental RAG indexing / dedup
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str] = mapped_column(String(80), default="text/plain")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    """Chunk metadata. Vectors live in the FAISS index keyed by chunk id."""

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)

    document: Mapped[Document] = relationship(back_populates="chunks")


Index("ix_chunks_doc_ordinal", DocumentChunk.document_id, DocumentChunk.ordinal)


class Conversation(Base):
    """Chat session (Apollo memory_service → tenant/project scoped)."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.id",
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    # Citations / retrieval provenance attached to an assistant reply.
    citations: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    model: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class PromptTemplate(Base):
    """Persisted prompt template library (Apollo PromptService)."""

    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(48))  # default, summarizer, ...
    name: Mapped[str] = mapped_column(String(120))
    template: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    is_system: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (
        Index("uq_prompt_org_key", "organization_id", "key", unique=False),
    )


# --------------------------------------------------------------------------
# generation (ComfyUI) — Create domain
# --------------------------------------------------------------------------
class Workflow(Base):
    """A ComfyUI workflow library entry (graph + logical input mapping)."""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)  # file stem
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GeneratedImage(Base):
    __tablename__ = "generated_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workflow_slug: Mapped[str] = mapped_column(String(120), default="", index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(String(255))
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lora_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lora_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Provenance chain: which training run produced the LoRA used here.
    source_training_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# --------------------------------------------------------------------------
# datasets (Argo) — image datasets for LoRA training
# --------------------------------------------------------------------------
class DatasetProject(Base):
    """Apollo LoRA 'project' split along the domain line: the image dataset
    lives here; the training orchestration lives in TrainingProject."""

    __tablename__ = "dataset_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    trigger_word: Mapped[str] = mapped_column(String(64), default="")
    caption_model: Mapped[str] = mapped_column(String(80), default="")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    images: Mapped[list["DatasetImage"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class DatasetImage(Base):
    __tablename__ = "dataset_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_projects.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    caption: Mapped[str] = mapped_column(Text, default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    captioned_by: Mapped[str] = mapped_column(String(40), default="")  # human | model name
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    dataset: Mapped[DatasetProject] = relationship(back_populates="images")


# --------------------------------------------------------------------------
# training (Leonidas) — LoRA training orchestration
# --------------------------------------------------------------------------
class TrainingProject(Base):
    __tablename__ = "training_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    dataset_id: Mapped[int | None] = mapped_column(
        ForeignKey("dataset_projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    preset: Mapped[str] = mapped_column(String(48), default="character")
    base_model: Mapped[str] = mapped_column(String(120), default="sd_xl_base_1.0")
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    runs: Mapped[list["TrainingRun"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class TrainingRunStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("training_projects.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(24), default=TrainingRunStatus.QUEUED, index=True
    )
    stage: Mapped[str] = mapped_column(String(120), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    log_tail: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    output_lora_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    project: Mapped[TrainingProject] = relationship(back_populates="runs")


class LoraAsset(Base):
    """A trained or imported LoRA weight discoverable by generation."""

    __tablename__ = "lora_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    path: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    source_training_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_model: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


__all__ = [
    "Conversation",
    "ConversationMessage",
    "DatasetImage",
    "DatasetProject",
    "Document",
    "DocumentChunk",
    "GeneratedImage",
    "LoraAsset",
    "PromptTemplate",
    "TrainingProject",
    "TrainingRun",
    "TrainingRunStatus",
    "Workflow",
]

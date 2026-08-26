"""RAG orchestration: incremental indexing, retrieval, context formatting.

Adapted from Apollo `RAGService` — proven incremental-indexing logic keyed by
content hash + embedding model identity, restart-safe persistence, and
citation-friendly retrieval.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.documents.chunking import chunk_text
from app.documents.embeddings import EmbeddingClient
from app.documents.vector_store import (
    IndexIncompatibleError,
    RetrievedChunk,
    StoredChunk,
    VectorStore,
)

logger = logging.getLogger(__name__)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RAGService:
    """Incremental FAISS index over documents with content-hash dedup."""

    def __init__(
        self,
        embedding_client: EmbeddingClient | None = None,
        persist_dir: str | Path | None = None,
    ) -> None:
        self.embedding_client = embedding_client or EmbeddingClient()
        self.persist_dir = Path(persist_dir) if persist_dir else settings.rag_dir
        self._store: VectorStore | None = None
        self._versions: dict[str, dict[str, Any]] = {}
        # Reentrant: add_document() holds the lock and calls remove_document(),
        # which takes it again. A plain Lock deadlocks on every upload.
        self._lock = threading.RLock()

    # ---------- public API ----------

    @property
    def chunk_count(self) -> int:
        return self._ensure_store().size

    @property
    def document_versions(self) -> dict[str, dict[str, Any]]:
        return dict(self._versions)

    def is_indexed_current(self, doc_id: str, text: str) -> bool:
        version = self._versions.get(doc_id)
        if version is None:
            return False
        return version.get("hash") == content_hash(text)

    def status(self) -> dict[str, Any]:
        store = self._ensure_store()
        return {
            "chunks": store.size,
            "documents": len(self._versions),
            "embedding_backend": self.embedding_client.backend,
            "embedding_model": self.embedding_client.model,
            "persist_dir": str(self.persist_dir),
            "document_versions": self.document_versions,
        }

    def add_document(
        self,
        doc_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> int:
        """Index a document. Skips work when an identical version is indexed.

        Returns the number of chunks added (0 when already current).
        """
        meta = dict(metadata or {})
        filename = str(meta.get("filename", doc_id))
        digest = content_hash(text)

        with self._lock:
            if not force and self.is_indexed_current(doc_id, text):
                return 0

            # Remove any previous version of this document first.
            self.remove_document(doc_id)

            chunks = chunk_text(
                text,
                chunk_size=settings.rag_chunk_size,
                overlap=settings.rag_chunk_overlap,
            )
            if not chunks:
                self._record_version(doc_id, digest, 0)
                return 0

            embeddings = self.embedding_client.embed_batch(chunks)
            stored = [
                StoredChunk(
                    doc_id=doc_id,
                    filename=filename,
                    chunk_index=i,
                    text=chunk,
                    content_hash=digest,
                )
                for i, chunk in enumerate(chunks)
            ]
            store = self._ensure_store(int(embeddings.shape[1]))
            store.add(embeddings, stored)
            self._record_version(doc_id, digest, len(chunks))
            store.save()
            logger.info("[RAG] Indexed '%s': %d chunks", doc_id, len(chunks))
            return len(chunks)

    def query(self, question: str, top_k: int | None = None) -> list[RetrievedChunk]:
        k = top_k or settings.rag_top_k
        store = self._ensure_store()
        if store.size == 0:
            return []
        embedding = self.embedding_client.embed(question)
        return store.search(embedding, top_k=k)

    def query_debug(self, question: str, top_k: int | None = None) -> dict[str, Any]:
        results = self.query(question, top_k=top_k)
        return {
            "question": question,
            "results": [
                {
                    "doc_id": r.doc_id,
                    "filename": r.filename,
                    "chunk_index": r.chunk_index,
                    "score": r.score,
                    "text": r.text,
                }
                for r in results
            ],
        }

    def format_context(self, chunks: list[RetrievedChunk]) -> str:
        """Render retrieved chunks as a cited context block for the LLM."""
        if not chunks:
            return ""
        parts = []
        for i, chunk in enumerate(chunks, start=1):
            parts.append(f"[{i}] Source: {chunk.filename} (chunk {chunk.chunk_index})\n{chunk.text}")
        return "\n\n".join(parts)

    def remove_document(self, doc_id: str) -> int:
        with self._lock:
            removed = self._ensure_store().remove_by_doc_id(doc_id)
            self._versions.pop(doc_id, None)
            if removed:
                self._ensure_store().save()
            return removed

    def clear(self) -> None:
        with self._lock:
            self._ensure_store().clear()
            self._versions.clear()

    # ---------- internals ----------

    def _ensure_store(self, dimension: int | None = None) -> VectorStore:
        if self._store is not None:
            return self._store
        dim = dimension or self.embedding_client.dimension
        try:
            self._store = VectorStore(
                dimension=dim,
                persist_dir=self.persist_dir,
                embedding_model=self.embedding_client.model,
            )
        except IndexIncompatibleError as exc:
            logger.warning("[RAG] Stored index incompatible (%s); rebuilding.", exc)
            self._quarantine_files()
            self._store = VectorStore(dimension=dim, persist_dir=None)
        self._rebuild_version_map()
        return self._store

    def _quarantine_files(self) -> None:
        import shutil
        import time

        quarantine = self.persist_dir / "quarantined"
        quarantine.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        for name in ("index.faiss", "chunks.json", "meta.json"):
            path = self.persist_dir / name
            if path.is_file():
                try:
                    shutil.move(str(path), str(quarantine / f"{name}.{stamp}"))
                except OSError as exc:
                    logger.warning("[RAG] Could not quarantine %s: %s", name, exc)

    def _rebuild_version_map(self) -> None:
        """Reconstruct per-document versions from persisted chunk metadata."""
        self._versions = {}
        store = self._store
        if store is None:
            return
        for chunk in store._chunks:  # noqa: SLF001 — same-package access
            entry = self._versions.setdefault(
                chunk.doc_id,
                {"hash": "", "chunks": 0, "filename": chunk.filename},
            )
            entry["hash"] = chunk.content_hash or entry["hash"]
            entry["chunks"] += 1

    def _record_version(self, doc_id: str, digest: str, chunk_count: int) -> None:
        self._versions[doc_id] = {"hash": digest, "chunks": chunk_count}

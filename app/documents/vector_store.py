"""Persistent FAISS vector store for RAG chunks.

Ported from Apollo `app/services/rag/vector_store.py` (proven logic,
unchanged behavior). Layout under ``DATA_DIR/rag/``::

    rag/index.faiss        flat inner-product index over normalized vectors
    rag/chunks.json        chunk metadata, one record per vector row
    rag/meta.json          index version, embedding model, dimension, counts

Atomic saves; self-describing metadata (model/dimension mismatch is refused,
never silently mixed); corruption is quarantined rather than deleted.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import faiss
import numpy as np

logger = logging.getLogger(__name__)

INDEX_VERSION = 1


class IndexIncompatibleError(Exception):
    """The stored index does not match the current embedding configuration."""


@dataclass
class StoredChunk:
    doc_id: str
    filename: str
    chunk_index: int
    text: str
    content_hash: str = ""


@dataclass
class RetrievedChunk:
    doc_id: str
    filename: str
    chunk_index: int
    text: str
    score: float


@dataclass
class IndexMeta:
    version: int = INDEX_VERSION
    embedding_model: str = ""
    dimension: int = 0
    vector_count: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IndexMeta:
        return cls(
            version=int(data.get("version", 0)),
            embedding_model=str(data.get("embedding_model", "")),
            dimension=int(data.get("dimension", 0)),
            vector_count=int(data.get("vector_count", 0)),
            updated_at=float(data.get("updated_at", 0.0)),
        )


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


class VectorStore:
    """FAISS-backed persistent vector store with chunk metadata."""

    def __init__(
        self,
        dimension: int,
        persist_dir: str | Path | None = None,
        embedding_model: str = "",
    ) -> None:
        self.dimension = dimension
        self.embedding_model = embedding_model
        self.persist_dir = Path(persist_dir) if persist_dir else None
        self._chunks: list[StoredChunk] = []
        self._embeddings: list[np.ndarray] = []
        self._index = faiss.IndexFlatIP(dimension)
        self._dirty = False

        if self.persist_dir is not None:
            self._load()

    # ---------- persistence ----------

    @property
    def _index_path(self) -> Path:
        assert self.persist_dir is not None
        return self.persist_dir / "index.faiss"

    @property
    def _chunks_path(self) -> Path:
        assert self.persist_dir is not None
        return self.persist_dir / "chunks.json"

    @property
    def _meta_path(self) -> Path:
        assert self.persist_dir is not None
        return self.persist_dir / "meta.json"

    def _load(self) -> bool:
        """Load a saved index. Returns True when usable state was restored."""
        assert self.persist_dir is not None
        meta_raw = _read_json(self._meta_path, default=None)
        if not isinstance(meta_raw, dict):
            return False

        meta = IndexMeta.from_dict(meta_raw)

        if meta.version != INDEX_VERSION:
            logger.warning("[RAG] Index version %s != %s; starting fresh", meta.version, INDEX_VERSION)
            self._quarantine()
            return False

        if meta.embedding_model != self.embedding_model:
            raise IndexIncompatibleError(
                f"Saved index was built with embedding model "
                f"'{meta.embedding_model}' but the current model is "
                f"'{self.embedding_model}'. Clear the RAG index to rebuild it."
            )

        if meta.dimension != self.dimension:
            raise IndexIncompatibleError(
                f"Saved index has dimension {meta.dimension} but the current "
                f"model produces {self.dimension}."
            )

        chunks_raw = _read_json(self._chunks_path, default=None)
        if not isinstance(chunks_raw, list) or len(chunks_raw) != meta.vector_count:
            logger.warning("[RAG] Chunk metadata missing/mismatched; rebuilding")
            self._quarantine()
            return False

        try:
            index = faiss.read_index(str(self._index_path))
        except (RuntimeError, OSError) as exc:
            logger.warning("[RAG] Could not read FAISS index (%s); quarantining", exc)
            self._quarantine()
            return False

        if index.ntotal != meta.vector_count or index.d != self.dimension:
            logger.warning("[RAG] Index shape mismatch; quarantining")
            self._quarantine()
            return False

        chunks: list[StoredChunk] = []
        for raw in chunks_raw:
            if not isinstance(raw, dict) or "doc_id" not in raw:
                continue
            chunks.append(StoredChunk(
                doc_id=str(raw["doc_id"]),
                filename=str(raw.get("filename", "")),
                chunk_index=int(raw.get("chunk_index", 0)),
                text=str(raw.get("text", "")),
                content_hash=str(raw.get("content_hash", "")),
            ))

        if len(chunks) != meta.vector_count:
            logger.warning("[RAG] Chunk records incomplete; quarantining")
            self._quarantine()
            return False

        self._index = index
        self._chunks = chunks
        self._embeddings = []
        self._dirty = False
        logger.info("[RAG] Restored index: %d vectors from %s", meta.vector_count, self.persist_dir)
        return True

    def save(self) -> None:
        """Flush index + metadata atomically."""
        if self.persist_dir is None or not self._dirty:
            return
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        faiss.write_index(self._index, str(self._index_path))
        _write_json_atomic(self._chunks_path, [asdict(chunk) for chunk in self._chunks])
        _write_json_atomic(
            self._meta_path,
            IndexMeta(
                embedding_model=self.embedding_model,
                dimension=self.dimension,
                vector_count=self._index.ntotal,
            ).to_dict(),
        )
        self._dirty = False

    def _quarantine(self) -> None:
        assert self.persist_dir is not None
        quarantine = self.persist_dir / "quarantined"
        quarantine.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        for path in (self._index_path, self._chunks_path, self._meta_path):
            if path.is_file():
                try:
                    shutil.move(str(path), str(quarantine / f"{path.name}.{stamp}"))
                except OSError as exc:
                    logger.warning("[RAG] Could not quarantine %s: %s", path.name, exc)
        logger.warning("[RAG] Quarantined unusable index files in %s", quarantine)

    # ---------- vector operations ----------

    @property
    def size(self) -> int:
        return len(self._chunks)

    @property
    def indexed_doc_ids(self) -> set[str]:
        return {chunk.doc_id for chunk in self._chunks}

    def add(self, embeddings: np.ndarray, chunks: list[StoredChunk]) -> None:
        if embeddings.size == 0:
            return
        if embeddings.shape[0] != len(chunks):
            raise ValueError("Embedding count must match chunk count")
        if embeddings.shape[1] != self.dimension:
            raise ValueError(f"Expected dimension {self.dimension}, got {embeddings.shape[1]}")

        self._index.add(embeddings.astype(np.float32))
        for row, chunk in zip(embeddings, chunks, strict=True):
            self._embeddings.append(np.asarray(row, dtype=np.float32))
            self._chunks.append(chunk)
        self._dirty = True

    def search(self, query_embedding: np.ndarray, top_k: int = 4) -> list[RetrievedChunk]:
        if self._index.ntotal == 0:
            return []

        vector = np.asarray(query_embedding, dtype=np.float32).reshape(1, -1)
        k = min(top_k, self._index.ntotal)
        scores, indices = self._index.search(vector, k)

        results: list[RetrievedChunk] = []
        for score, idx in zip(scores[0], indices[0], strict=True):
            if idx < 0:
                continue
            chunk = self._chunks[idx]
            results.append(RetrievedChunk(
                doc_id=chunk.doc_id,
                filename=chunk.filename,
                chunk_index=chunk.chunk_index,
                text=chunk.text,
                score=float(score),
            ))
        return results

    def remove_by_doc_id(self, doc_id: str) -> int:
        remaining_chunks: list[StoredChunk] = []
        remaining_embeddings: list[np.ndarray] = []
        removed = 0

        for chunk, embedding in zip(self._chunks, self._embeddings, strict=True):
            if chunk.doc_id == doc_id:
                removed += 1
                continue
            remaining_chunks.append(chunk)
            remaining_embeddings.append(embedding)

        if removed:
            self._rebuild(remaining_chunks, remaining_embeddings)
            self._dirty = True
        return removed

    def clear(self) -> None:
        self._chunks.clear()
        self._embeddings.clear()
        self._index = faiss.IndexFlatIP(self.dimension)
        self._dirty = True
        if self.persist_dir is not None:
            for path in (self._index_path, self._chunks_path, self._meta_path):
                path.unlink(missing_ok=True)

    def _rebuild(self, chunks: list[StoredChunk], embeddings: list[np.ndarray]) -> None:
        self._chunks = chunks
        self._embeddings = embeddings
        self._index = faiss.IndexFlatIP(self.dimension)
        if embeddings:
            matrix = np.vstack(embeddings).astype(np.float32)
            self._index.add(matrix)

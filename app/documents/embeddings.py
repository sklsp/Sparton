"""Embedding generation via the unified LLM layer.

Adapted from Apollo `EmbeddingClient`: Ollama embeddings first, local
sentence-transformers fallback. Rewired to SPARTON's provider abstraction
(`app.llm`) instead of Apollo's standalone OllamaClient.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any

import numpy as np

from app.core.config import settings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

#: Vector width of the offline hash backend. Fixed so a persisted index built
#: in one process is still readable in the next.
HASH_EMBEDDING_DIM = 256

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class EmbeddingError(Exception):
    """Raised when no embedding backend is available."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EmbeddingClient:
    """Generate text embeddings using Ollama, sentence-transformers, or a
    deterministic offline hash projection.

    The hash backend is the last resort and the one the test suite uses. It
    needs no network and no model download, which is what keeps `pytest` from
    hanging on a HuggingFace fetch (see docs/DECISIONS.md D-025).
    """

    def __init__(
        self,
        model: str | None = None,
        fallback_model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model or settings.embedding_model
        self.fallback_model = fallback_model or settings.embedding_fallback_model
        self.base_url = base_url or settings.ollama_base_url
        self._backend: str | None = None
        self._st_model: SentenceTransformer | None = None
        self._dimension: int | None = None

    @property
    def dimension(self) -> int:
        """Embedding vector size for the active backend."""
        if self._dimension is None:
            self._dimension = int(self.embed("dimension probe").shape[0])
        return self._dimension

    @property
    def backend(self) -> str:
        """Active embedding backend name."""
        if self._backend is None:
            self.embed("backend probe")
        return self._backend or "unknown"

    def embed(self, text: str) -> np.ndarray:
        vectors = self.embed_batch([text])
        return vectors[0]

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, 0), dtype=np.float32)

        # `settings.embedding_backend` forces a backend. "auto" walks the chain.
        wanted = (settings.embedding_backend or "auto").lower()
        attempts: list[tuple[str, Any]] = []
        if wanted in ("auto", "ollama"):
            attempts.append(("ollama", self._embed_with_ollama))
        if wanted in ("auto", "sentence-transformers"):
            attempts.append(("sentence-transformers", self._embed_with_sentence_transformers))
        attempts.append(("hash", self._embed_with_hash))

        for backend, fn in attempts:
            try:
                vectors = fn(texts)
            except Exception as exc:  # noqa: BLE001 — any failure falls through
                logger.warning("Embedding backend %s unavailable (%s).", backend, exc)
                continue
            self._backend = backend
            self._dimension = int(vectors.shape[1])
            return _normalize(vectors.astype(np.float32))

        raise EmbeddingError("No embedding backend is available")

    def _embed_with_ollama(self, texts: list[str]) -> np.ndarray:
        import requests

        response = requests.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings")
        if not embeddings:
            raise EmbeddingError(f"Ollama returned no embeddings: {data}")
        return np.asarray(embeddings, dtype=np.float32)

    def _embed_with_sentence_transformers(self, texts: list[str]) -> np.ndarray:
        if self._st_model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise EmbeddingError(
                    "Embedding fallback unavailable: install sentence-transformers"
                ) from exc
            self._st_model = SentenceTransformer(self.fallback_model)

        vectors = self._st_model.encode(
            texts, convert_to_numpy=True, show_progress_bar=False
        )
        return np.asarray(vectors, dtype=np.float32)

    def _embed_with_hash(self, texts: list[str]) -> np.ndarray:
        """Deterministic, offline, no-download bag-of-words projection.

        Each token is hashed to a coordinate and its count accumulated; the
        caller then L2-normalizes. Two identical texts always produce identical
        vectors, which is all the test suite needs — similarity quality is
        deliberately mediocre (see docs/DECISIONS.md D-025).
        """
        vectors = np.zeros((len(texts), HASH_EMBEDDING_DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in _TOKEN_RE.findall(text.lower()):
                # blake2b is stable across processes and platforms, unlike
                # hash() which is salted per interpreter run.
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % HASH_EMBEDDING_DIM
                vectors[row, index] += 1.0 if digest[4] & 1 else -1.0
        return vectors


def _normalize(vectors: np.ndarray) -> np.ndarray:
    """L2-normalize rows for cosine similarity via inner product."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms

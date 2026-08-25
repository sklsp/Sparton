"""Embedding generation via the unified LLM layer.

Adapted from Apollo `EmbeddingClient`: Ollama embeddings first, local
sentence-transformers fallback. Rewired to SPARTON's provider abstraction
(`app.llm`) instead of Apollo's standalone OllamaClient.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

from app.core.config import settings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Raised when no embedding backend is available."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EmbeddingClient:
    """Generate text embeddings using Ollama or a local sentence-transformers model."""

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

        try:
            vectors = self._embed_with_ollama(texts)
            self._backend = "ollama"
        except Exception as exc:  # noqa: BLE001 — any Ollama failure falls back
            logger.warning("Ollama embeddings unavailable (%s); using fallback.", exc)
            vectors = self._embed_with_sentence_transformers(texts)
            self._backend = "sentence-transformers"

        self._dimension = int(vectors.shape[1])
        return _normalize(vectors.astype(np.float32))

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


def _normalize(vectors: np.ndarray) -> np.ndarray:
    """L2-normalize rows for cosine similarity via inner product."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms

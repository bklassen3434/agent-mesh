"""Text embeddings for entity resolution (Phase 13a).

A small ``Embedder`` Protocol with a single batched ``embed`` method, plus the
default ``FastEmbedEmbedder`` backed by ONNX (``fastembed``) — arm64-native, no
torch, runs offline once the model is cached. The default model
``BAAI/bge-small-en-v1.5`` is 384-dimensional, matching the long-reserved
``entities.name_embedding vector(384)`` column.

The Protocol keeps the embedding backend swappable so tests inject a
deterministic fake (no model download in CI). ``make_embedder()`` is the
production factory.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384


def entity_embed_text(name: str, entity_type: str) -> str:
    """Normalized text that represents an entity for embedding.

    The name carries the signal; the type disambiguates near-namesakes across
    kinds (e.g. a "GPT-4" model vs a "GPT-4" paper). Backfill, reconciliation,
    and the live resolver MUST all embed via this function — if the
    representation diverges, blocking silently degrades.
    """
    return f"{name.strip()} ({entity_type})"


@runtime_checkable
class Embedder(Protocol):
    """Maps texts to fixed-dimension vectors. Implementations must batch."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedEmbedder:
    """ONNX-backed embedder (``fastembed``). The model is constructed lazily on
    first ``embed`` and reused for the process lifetime."""

    def __init__(self, model_name: str = DEFAULT_EMBED_MODEL) -> None:
        self.model_name = model_name
        self._model: object | None = None

    def _ensure_model(self) -> object:
        if self._model is None:
            # Lazy import: importing mesh_llm must not require fastembed loaded.
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        # fastembed yields numpy arrays; materialize to plain float lists.
        return [list(map(float, vec)) for vec in model.embed(texts)]  # type: ignore[attr-defined]


def make_embedder() -> Embedder:
    """Production embedder selected by ``MESH_EMBED_MODEL`` (default bge-small)."""
    model = (os.environ.get("MESH_EMBED_MODEL") or "").strip() or DEFAULT_EMBED_MODEL
    return FastEmbedEmbedder(model_name=model)

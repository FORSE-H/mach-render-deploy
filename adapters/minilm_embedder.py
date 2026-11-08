"""MiniLMEmbedder — the Embedder port backed by fastembed / all-MiniLM-L6-v2.

The only module that imports fastembed. The model (384-dim, Apache-2.0) is
loaded lazily on first embed so importing this module — and constructing the
object — stays cheap; nothing downloads weights until an embedding is actually
requested.
"""

from __future__ import annotations

from core.embedding import EMBEDDING_DIM

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


class MiniLMEmbedder:
    def __init__(self, model_name: str = _MODEL_NAME) -> None:
        self.model_name = model_name
        self._model_name = model_name
        self._model = None  # loaded on first use

    def _ensure_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        vectors = [vec.tolist() for vec in model.embed(texts)]
        for v in vectors:
            if len(v) != EMBEDDING_DIM:
                raise ValueError(f"expected dim {EMBEDDING_DIM}, got {len(v)}")
        return vectors

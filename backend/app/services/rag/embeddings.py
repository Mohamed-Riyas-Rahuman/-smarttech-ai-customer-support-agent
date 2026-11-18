"""Configured local sentence-transformers model for PostgreSQL pgvector."""
import os
from app.core.config import settings

# Prefer local cached models to avoid blocking DNS/network retries
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")



class EmbeddingService:
    def __init__(self, model_name: str | None = None, dimension: int | None = None):
        self.model_name = model_name or settings.embedding_model
        self.dimension = dimension or settings.embedding_dimension
        self._model = None

    def _get_model(self):
        if self._model is None:
            if settings.embedding_provider.lower() != "huggingface":
                raise ValueError(
                    f"Unsupported embedding provider: {settings.embedding_provider}"
                )

            from sentence_transformers import SentenceTransformer

            try:
                self._model = SentenceTransformer(
                    self.model_name,
                    device="cpu",
                    model_kwargs={"local_files_only": True},
                )
            except Exception:
                try:
                    self._model = SentenceTransformer(
                        self.model_name,
                        device="cpu",
                        local_files_only=True,
                    )
                except Exception:
                    self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    def get_embedding(self, text: str) -> list[float]:
        """Generate an embedding and enforce the configured database dimension."""
        vector = self._get_model().encode(
            text,
            normalize_embeddings=True,
            convert_to_numpy=True,
        ).tolist()
        if len(vector) != self.dimension:
            raise ValueError(
                f"Embedding model {self.model_name} returned {len(vector)} dimensions; "
                f"EMBEDDING_DIMENSION is {self.dimension}."
            )
        return vector

    def get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch and enforce the configured database dimension."""
        vectors = self._get_model().encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
        ).tolist()
        if any(len(vector) != self.dimension for vector in vectors):
            raise ValueError(
                f"Embedding model {self.model_name} did not return "
                f"{self.dimension}-dimensional vectors."
            )
        return vectors


embedding_service = EmbeddingService()

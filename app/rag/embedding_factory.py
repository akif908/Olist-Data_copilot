"""Create the configured embedding service.

The single place that knows which embedding providers exist. To add a provider:
implement EmbeddingService, then register it here. The configured model is used
verbatim — there is no silent fallback to a different model.
"""
from __future__ import annotations

from app.config import EmbeddingSettings
from app.rag.embeddings import (
    DEFAULT_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LOCAL_MODEL,
    EmbeddingError,
    EmbeddingService,
    LocalSentenceTransformerEmbedding,
    OpenRouterEmbeddingService,
)


def create_embedding_service(settings: EmbeddingSettings | None = None) -> EmbeddingService:
    settings = settings or EmbeddingSettings.from_env()
    provider = settings.provider
    if provider == "openrouter":
        return OpenRouterEmbeddingService(
            api_key=settings.api_key,
            model=settings.model or DEFAULT_EMBEDDING_MODEL,
            base_url=settings.base_url or DEFAULT_BASE_URL,
        )
    if provider in {"local", "sentence-transformers", "sentence_transformers"}:
        return LocalSentenceTransformerEmbedding(
            model_name=settings.model or DEFAULT_LOCAL_MODEL,
        )
    raise EmbeddingError(
        f"Unsupported EMBEDDING_PROVIDER '{provider}'. Supported: openrouter, local."
    )
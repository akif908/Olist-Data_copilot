"""Embed knowledge chunks and persist them in ChromaDB.

Persistence: a ChromaDB PersistentClient writes to ``chroma_db/`` on disk, so the
index survives process restarts (verified by scripts/verify_rag_persistence.py).

No silent model switch: the embedding model name, provider, and dimension are
stored in the collection metadata. Re-ingesting with a *different* model raises
IngestionError unless ``reset=True`` is passed, because vectors from different
models are not comparable. Embeddings are always supplied explicitly
(``embedding_function=None``) so ChromaDB never downloads its own ONNX model.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import chromadb

from app.agent.sql_generator import KnowledgeBase
from app.config import PROJECT_ROOT
from app.rag.chunking import Chunk, build_chunks
from app.rag.embedding_factory import create_embedding_service
from app.rag.embeddings import EmbeddingService

CHROMA_DIR = PROJECT_ROOT / "chroma_db"
COLLECTION_NAME = "olist_knowledge"


class IngestionError(Exception):
    """Ingestion failed or the stored index was built with a different model."""


@dataclass(frozen=True)
class IngestionResult:
    collection: str
    persist_dir: str
    provider: str
    model: str
    dimension: int
    chunk_count: int
    by_type: dict[str, int]


def _model_metadata(svc: EmbeddingService, dimension: int) -> dict:
    return {
        "embedding_model": svc.name,
        "embedding_provider": svc.provider,
        "embedding_dimension": dimension,
        "hnsw:space": "cosine",
    }


def ingest(
    kb: KnowledgeBase | None = None,
    embedding_service: EmbeddingService | None = None,
    persist_dir: Path = CHROMA_DIR,
    collection_name: str = COLLECTION_NAME,
    reset: bool = False,
) -> IngestionResult:
    """Build/refresh the persisted vector index. Idempotent for the same model.

    ``reset=True`` deletes and rebuilds the collection (required when switching
    embedding models). Without it, a model/dimension mismatch raises rather than
    silently mixing incomparable vectors.
    """
    kb = kb or KnowledgeBase.load()
    svc = embedding_service or create_embedding_service()
    chunks: list[Chunk] = build_chunks(kb)
    if not chunks:
        raise IngestionError("knowledge base produced no chunks")

    texts = [c.text for c in chunks]
    vectors = svc.embed(texts)  # also discovers the real dimension
    if len(vectors) != len(chunks):
        raise IngestionError("embedding count does not match chunk count")
    dimension = svc.dimension

    client = chromadb.PersistentClient(path=str(persist_dir))

    existing = None
    try:
        existing = client.get_collection(collection_name, embedding_function=None)
    except Exception:
        existing = None

    if existing is not None:
        em = existing.metadata or {}
        old_model = em.get("embedding_model")
        old_dim = em.get("embedding_dimension")
        mismatch = (old_model is not None and old_model != svc.name) or (
            old_dim is not None and int(old_dim) != dimension
        )
        if mismatch and not reset:
            raise IngestionError(
                f"Collection '{collection_name}' was built with model "
                f"'{old_model}' (dim {old_dim}); current model is '{svc.name}' "
                f"(dim {dimension}). Re-run with reset=True to rebuild — vectors "
                f"from different embedding models are not comparable."
            )
        if reset:
            client.delete_collection(collection_name)

    collection = client.get_or_create_collection(
        name=collection_name,
        metadata=_model_metadata(svc, dimension),
        embedding_function=None,
    )
    collection.upsert(
        ids=[c.id for c in chunks],
        embeddings=vectors,
        documents=texts,
        metadatas=[c.to_metadata() for c in chunks],
    )

    # Prune any stale ids no longer present in the knowledge base.
    current_ids = {c.id for c in chunks}
    stored = collection.get(include=[])
    stale = [i for i in stored["ids"] if i not in current_ids]
    if stale:
        collection.delete(ids=stale)

    by_type = dict(Counter(c.type for c in chunks))
    return IngestionResult(
        collection=collection_name,
        persist_dir=str(persist_dir),
        provider=svc.provider,
        model=svc.name,
        dimension=dimension,
        chunk_count=collection.count(),
        by_type=by_type,
    )
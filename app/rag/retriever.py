"""Semantic retrieval over the persisted knowledge index.

The Retriever opens the ChromaDB collection built by ingestion (read-only), embeds
the user question with the *same* configured model, and returns the top-k chunks
as inspectable, scored sources. It refuses to query an index built with a
different embedding model — there is no silent switch.

Each RetrievedChunk carries its provenance (type, source_id, tables, metrics) and
a cosine similarity score, so the SQL generator and the UI can show exactly which
knowledge grounded a query.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import chromadb

from app.config import PROJECT_ROOT
from app.rag.embedding_factory import create_embedding_service
from app.rag.embeddings import EmbeddingService
from app.rag.ingestion import CHROMA_DIR, COLLECTION_NAME


class RetrievalError(Exception):
    """Retrieval failed or the index/model is inconsistent."""


@dataclass(frozen=True)
class RetrievedChunk:
    id: str
    text: str
    type: str
    source_id: str
    tables: tuple[str, ...]
    metrics: tuple[str, ...]
    distance: float
    score: float  # cosine similarity = 1 - cosine distance

    def label(self) -> str:
        return f"{self.type}:{self.source_id}"


def _split_csv(value: str | None) -> tuple[str, ...]:
    return tuple(p for p in (value or "").split(",") if p)


class Retriever:
    """Read-only semantic retriever backed by a persisted ChromaDB collection."""

    def __init__(
        self,
        embedding_service: EmbeddingService | None = None,
        persist_dir: Path = CHROMA_DIR,
        collection_name: str = COLLECTION_NAME,
    ) -> None:
        self._svc = embedding_service or create_embedding_service()
        self._persist_dir = Path(persist_dir)
        self._collection_name = collection_name
        try:
            self._client = chromadb.PersistentClient(path=str(self._persist_dir))
            self._col = self._client.get_collection(
                collection_name, embedding_function=None
            )
        except Exception as exc:
            raise RetrievalError(
                f"Could not open knowledge collection '{collection_name}' at "
                f"{self._persist_dir}. Run: python -m scripts.ingest_knowledge"
            ) from exc

        em = self._col.metadata or {}
        stored_model = em.get("embedding_model")
        if stored_model and stored_model != self._svc.name:
            raise RetrievalError(
                f"Index '{collection_name}' was built with model '{stored_model}' "
                f"but the configured embedding model is '{self._svc.name}'. "
                f"Re-ingest with the matching model (or reset) — no silent switch."
            )

    @property
    def model(self) -> str:
        return self._svc.name

    @property
    def provider(self) -> str:
        return self._svc.provider

    @property
    def collection_name(self) -> str:
        return self._collection_name

    def count(self) -> int:
        return self._col.count()

    def retrieve(
        self,
        question: str,
        k: int = 8,
        where: dict | None = None,
    ) -> list[RetrievedChunk]:
        """Return the top-k most similar knowledge chunks for a question."""
        if k < 1:
            raise RetrievalError("k must be >= 1")
        if not question.strip():
            raise RetrievalError("question must not be empty")
        query_vec = self._svc.embed_one(question)
        res = self._col.query(
            query_embeddings=[query_vec],
            n_results=min(k, self.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        chunks: list[RetrievedChunk] = []
        ids = res["ids"][0]
        docs = res["documents"][0]
        metas = res["metadatas"][0]
        dists = res["distances"][0]
        for cid, doc, meta, dist in zip(ids, docs, metas, dists):
            chunks.append(
                RetrievedChunk(
                    id=cid,
                    text=doc,
                    type=meta.get("type", ""),
                    source_id=meta.get("source_id", ""),
                    tables=_split_csv(meta.get("tables")),
                    metrics=_split_csv(meta.get("metrics")),
                    distance=float(dist),
                    score=1.0 - float(dist),
                )
            )
        return chunks


def retrieved_context(chunks: list[RetrievedChunk]) -> str:
    """Format retrieved chunks as grounding context for the SQL generator."""
    lines = ["=== RETRIEVED KNOWLEDGE (most relevant first) ==="]
    for c in chunks:
        lines.append(f"\n[{c.label()} | similarity {c.score:.3f}]")
        lines.append(c.text)
    return "\n".join(lines)
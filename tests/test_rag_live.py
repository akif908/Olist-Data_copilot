"""Live Phase 4 verification against the real OpenRouter embeddings API.

Skipped automatically when no OpenRouter API key is configured or the embedding
provider is not OpenRouter. Consumes a small amount of API quota (one ingestion
batch + a few queries). Verifies the real embedding dimension, document/query
embedding generation, and that retrieval surfaces the right knowledge.
"""
from __future__ import annotations

import pytest

from app.config import EmbeddingSettings
from app.rag.embedding_factory import create_embedding_service
from app.rag.embeddings import DEFAULT_EMBEDDING_MODEL
from app.rag.ingestion import ingest
from app.rag.retriever import Retriever

_settings = EmbeddingSettings.from_env()
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        _settings.provider != "openrouter" or not _settings.api_key,
        reason="no OpenRouter embedding API key configured in .env",
    ),
]


@pytest.fixture(scope="module")
def live_svc():
    return create_embedding_service()


@pytest.fixture(scope="module")
def live_retriever(tmp_path_factory, live_svc):
    d = tmp_path_factory.mktemp("chroma_live")
    ingest(embedding_service=live_svc, persist_dir=d,
           collection_name="live_kb", reset=True)
    return Retriever(live_svc, persist_dir=d, collection_name="live_kb")


def test_live_document_and_query_embeddings(live_svc) -> None:
    docs = live_svc.embed(["orders table with status", "average order value metric"])
    query = live_svc.embed_one("what is the average order value?")
    assert len(docs) == 2
    assert len(docs[0]) == len(docs[1]) == len(query)
    assert len(query) > 0
    # The user-selected model is verified to be 2048-dimensional.
    if live_svc.name == DEFAULT_EMBEDDING_MODEL:
        assert len(query) == 2048


def test_live_retrieval_aov(live_retriever) -> None:
    labels = [c.label() for c in live_retriever.retrieve(
        "What is the average order value?", k=3)]
    assert ("metric:average_order_value" in labels
            or "example:ex_average_order_value" in labels)


def test_live_retrieval_unsupported_profit(live_retriever) -> None:
    labels = [c.label() for c in live_retriever.retrieve("What is our net profit?", k=3)]
    assert any(lab.startswith("unsupported_metric:") for lab in labels)
"""Phase 4 tests — RAG embeddings, chunking, ingestion, retrieval, integration.

All tests here run offline: a deterministic FakeEmbedding plus mocked httpx for
the OpenRouter transport. Live API verification lives in test_rag_live.py.
"""
from __future__ import annotations

import hashlib
import json
import math
from unittest.mock import Mock

import pytest

from app.agent.sql_generator import (
    KnowledgeBase,
    build_rag_prompt,
    generate_sql,
)
from app.config import EmbeddingSettings
from app.llm.base import LLMResult, LLMService
from app.rag.chunking import (
    EXAMPLE,
    METRIC,
    SCHEMA,
    UNSUPPORTED_METRIC,
    build_chunks,
)
from app.rag.embedding_factory import create_embedding_service
from app.rag.embeddings import (
    EmbeddingError,
    EmbeddingService,
    OpenRouterEmbeddingService,
)
from app.rag.ingestion import IngestionError, ingest
from app.rag.retriever import RetrievalError, Retriever, retrieved_context


# --- Deterministic fake embedding (same text -> same unit vector) ------------

class FakeEmbedding(EmbeddingService):
    provider = "fake"

    def __init__(self, dimension: int = 16, name: str = "fake-embed") -> None:
        self._dimension = dimension
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        v = [(h[i % len(h)] / 255.0) - 0.5 for i in range(self._dimension)]
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


class FakeLLM(LLMService):
    provider = "fake"

    def __init__(self, response: dict) -> None:
        self.response = response
        self.last_prompt: str | None = None

    def generate(self, prompt, *, system=None, temperature=0.0, max_tokens=2048):
        self.last_prompt = prompt
        return LLMResult(text=json.dumps(self.response), model="fake-model",
                         prompt_tokens=10, completion_tokens=5)


@pytest.fixture(scope="module")
def kb() -> KnowledgeBase:
    return KnowledgeBase.load()


# --- Config & factory --------------------------------------------------------

def test_embedding_settings_reuse_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "shared-key")
    monkeypatch.setenv("OPENROUTER_EMBEDDING_MODEL", "nvidia/nemotron-3-embed-1b:free")
    monkeypatch.delenv("OPENROUTER_EMBEDDING_API_KEY", raising=False)
    s = EmbeddingSettings.from_env()
    assert s.provider == "openrouter"
    assert s.api_key == "shared-key"  # reused from the LLM key
    assert s.model == "nvidia/nemotron-3-embed-1b:free"


def test_embedding_settings_dedicated_key_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "shared-key")
    monkeypatch.setenv("OPENROUTER_EMBEDDING_API_KEY", "embed-key")
    s = EmbeddingSettings.from_env()
    assert s.api_key == "embed-key"


def test_factory_openrouter() -> None:
    svc = create_embedding_service(EmbeddingSettings("openrouter", "k", "", ""))
    assert isinstance(svc, OpenRouterEmbeddingService)
    assert svc.name == "nvidia/nemotron-3-embed-1b:free"  # default, not swapped


def test_factory_unknown_provider() -> None:
    with pytest.raises(EmbeddingError, match="Unsupported EMBEDDING_PROVIDER"):
        create_embedding_service(EmbeddingSettings("nosuch", "k", "m", ""))


def test_openrouter_requires_key() -> None:
    with pytest.raises(EmbeddingError, match="OPENROUTER_API_KEY"):
        OpenRouterEmbeddingService(api_key="")


# --- Chunking ----------------------------------------------------------------

def test_chunk_counts_and_types(kb: KnowledgeBase) -> None:
    chunks = build_chunks(kb)
    by_type = {}
    for c in chunks:
        by_type[c.type] = by_type.get(c.type, 0) + 1
    assert by_type[SCHEMA] == len(kb.metadata["tables"]) == 7
    assert by_type[METRIC] == len(kb.glossary["metrics"]) == 9
    assert by_type[UNSUPPORTED_METRIC] == len(kb.glossary["unsupported_metrics"]) == 2
    assert by_type[EXAMPLE] == len(kb.examples) == 13
    assert len(chunks) == 31


def test_chunk_ids_unique(kb: KnowledgeBase) -> None:
    chunks = build_chunks(kb)
    ids = [c.id for c in chunks]
    assert len(ids) == len(set(ids))


def test_schema_chunk_has_columns_and_keys(kb: KnowledgeBase) -> None:
    chunks = {c.id: c for c in build_chunks(kb)}
    orders = chunks["schema:orders"]
    assert "order_status" in orders.text
    assert "PRIMARY KEY (order_id)" in orders.text
    assert "FOREIGN KEY" in orders.text
    assert orders.tables == ("orders",)


def test_metric_chunk_has_sql_and_caveats(kb: KnowledgeBase) -> None:
    chunks = {c.id: c for c in build_chunks(kb)}
    aov = chunks["metric:average_order_value"]
    assert "payment_value" in aov.text
    assert "Caveats:" in aov.text
    assert aov.metrics == ("average_order_value",)


def test_unsupported_chunk_present(kb: KnowledgeBase) -> None:
    chunks = {c.id: c for c in build_chunks(kb)}
    assert "unsupported_metric:profit" in chunks
    assert "must not be computed" in chunks["unsupported_metric:profit"].text


def test_metadata_is_scalar_csv(kb: KnowledgeBase) -> None:
    chunks = build_chunks(kb)
    for c in chunks:
        meta = c.to_metadata()
        assert all(isinstance(v, str) for v in meta.values())
    multi = next(c for c in chunks if len(c.tables) > 1)
    assert "," in multi.to_metadata()["tables"]


# --- OpenRouter embedding transport (mocked HTTP) ----------------------------

def _emb_response(status: int, vectors: list[list[float]], model: str = "m") -> Mock:
    body = {
        "object": "list",
        "data": [{"embedding": v, "index": i} for i, v in enumerate(vectors)],
        "model": model,
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    resp.headers = {}
    return resp


def test_embed_preserves_order_via_index(monkeypatch: pytest.MonkeyPatch) -> None:
    # API returns items out of order; embed() must sort by index.
    body = {"data": [{"embedding": [0.0, 1.0], "index": 1},
                     {"embedding": [1.0, 0.0], "index": 0}]}
    resp = Mock(status_code=200, headers={})
    resp.json.return_value = body
    resp.text = json.dumps(body)
    post = Mock(return_value=resp)
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    svc = OpenRouterEmbeddingService(api_key="k", model="my-model")
    out = svc.embed(["a", "b"])
    assert out == [[1.0, 0.0], [0.0, 1.0]]
    assert svc.dimension == 2
    # model used verbatim — no silent switch
    assert post.call_args.kwargs["json"]["model"] == "my-model"


def test_embed_empty_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock()
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    svc = OpenRouterEmbeddingService(api_key="k")
    assert svc.embed([]) == []
    post.assert_not_called()


def test_embed_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock(side_effect=lambda *a, **k: _emb_response(200, [[1.0, 0.0]]))
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    svc = OpenRouterEmbeddingService(api_key="k", batch_size=1)
    out = svc.embed(["a", "b", "c"])
    assert len(out) == 3
    assert post.call_count == 3


def test_dimension_discovered_by_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock(return_value=_emb_response(200, [[0.1, 0.2, 0.3, 0.4]]))
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    svc = OpenRouterEmbeddingService(api_key="k")
    assert svc.dimension == 4  # discovered, not assumed
    assert post.call_count == 1


def test_count_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock(return_value=_emb_response(200, [[1.0, 0.0]]))  # 1 vec for 2 inputs
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    svc = OpenRouterEmbeddingService(api_key="k")
    with pytest.raises(EmbeddingError, match="embeddings for"):
        svc.embed(["a", "b"])


def test_inconsistent_dimensions_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock(return_value=_emb_response(200, [[1.0, 0.0], [1.0, 0.0, 0.0]]))
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    svc = OpenRouterEmbeddingService(api_key="k")
    with pytest.raises(EmbeddingError, match="inconsistent"):
        svc.embed(["a", "b"])


def test_error_sanitized_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = Mock(status_code=401, headers={})
    resp.json.return_value = {"error": {"message": "Invalid key"}}
    resp.text = "Invalid key"
    post = Mock(return_value=resp)
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    svc = OpenRouterEmbeddingService(api_key="secret-key")
    with pytest.raises(EmbeddingError) as exc:
        svc.embed(["a"])
    assert "401" in str(exc.value)
    assert "secret-key" not in str(exc.value)


def test_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _emb_response(200, [[1.0, 0.0]])
    post = Mock(side_effect=[_emb_response(429, [[0.0]]), ok])
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    monkeypatch.setattr("app.rag.embeddings.time.sleep", lambda s: None)
    svc = OpenRouterEmbeddingService(api_key="k")
    assert svc.embed(["a"]) == [[1.0, 0.0]]
    assert post.call_count == 2


def test_retries_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _emb_response(200, [[1.0, 0.0]])
    post = Mock(side_effect=[_emb_response(503, [[0.0]]), ok])
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    monkeypatch.setattr("app.rag.embeddings.time.sleep", lambda s: None)
    svc = OpenRouterEmbeddingService(api_key="k")
    assert svc.embed(["a"]) == [[1.0, 0.0]]
    assert post.call_count == 2


def test_no_retry_on_400(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = Mock(status_code=400, headers={})
    resp.json.return_value = {"error": {"message": "bad model"}}
    resp.text = "bad model"
    post = Mock(return_value=resp)
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    svc = OpenRouterEmbeddingService(api_key="k")
    with pytest.raises(EmbeddingError, match="400"):
        svc.embed(["a"])
    assert post.call_count == 1  # config error: no retry, no fallback


def test_transport_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    post = Mock(side_effect=httpx.ConnectError("boom"))
    monkeypatch.setattr("app.rag.embeddings.httpx.post", post)
    monkeypatch.setattr("app.rag.embeddings.time.sleep", lambda s: None)
    svc = OpenRouterEmbeddingService(api_key="k", max_retries=2)
    with pytest.raises(EmbeddingError, match="request failed"):
        svc.embed(["a"])


# --- Ingestion & persistence (fake embeddings, temp dir) ---------------------

def test_ingest_creates_collection(kb: KnowledgeBase, tmp_path) -> None:
    svc = FakeEmbedding()
    result = ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb", reset=True)
    assert result.chunk_count == 31
    assert result.dimension == 16
    assert result.model == "fake-embed"
    assert result.by_type[SCHEMA] == 7


def test_ingest_idempotent(kb: KnowledgeBase, tmp_path) -> None:
    svc = FakeEmbedding()
    ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb", reset=True)
    again = ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb")
    assert again.chunk_count == 31  # upsert, no duplicates


def test_ingest_model_mismatch_raises(kb: KnowledgeBase, tmp_path) -> None:
    ingest(kb, FakeEmbedding(name="model-a"), persist_dir=tmp_path,
           collection_name="test_kb", reset=True)
    with pytest.raises(IngestionError, match="not comparable"):
        ingest(kb, FakeEmbedding(name="model-b"), persist_dir=tmp_path,
               collection_name="test_kb")


def test_ingest_dimension_mismatch_raises(kb: KnowledgeBase, tmp_path) -> None:
    ingest(kb, FakeEmbedding(dimension=16), persist_dir=tmp_path,
           collection_name="test_kb", reset=True)
    with pytest.raises(IngestionError, match="not comparable"):
        ingest(kb, FakeEmbedding(dimension=8), persist_dir=tmp_path,
               collection_name="test_kb")


def test_ingest_reset_switches_model(kb: KnowledgeBase, tmp_path) -> None:
    ingest(kb, FakeEmbedding(name="model-a"), persist_dir=tmp_path,
           collection_name="test_kb", reset=True)
    r = ingest(kb, FakeEmbedding(name="model-b"), persist_dir=tmp_path,
               collection_name="test_kb", reset=True)
    assert r.model == "model-b"
    assert r.chunk_count == 31


def test_persistence_across_clients(kb: KnowledgeBase, tmp_path) -> None:
    ingest(kb, FakeEmbedding(), persist_dir=tmp_path, collection_name="test_kb", reset=True)
    # A brand-new Retriever (new client) reads the persisted index, no re-ingest.
    r = Retriever(FakeEmbedding(), persist_dir=tmp_path, collection_name="test_kb")
    assert r.count() == 31


# --- Retriever ---------------------------------------------------------------

def test_retrieve_exact_text_ranks_first(kb: KnowledgeBase, tmp_path) -> None:
    svc = FakeEmbedding()
    ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb", reset=True)
    r = Retriever(svc, persist_dir=tmp_path, collection_name="test_kb")
    target = next(c for c in build_chunks(kb) if c.id == "metric:average_order_value")
    chunks = r.retrieve(target.text, k=3)
    assert chunks[0].id == "metric:average_order_value"
    assert chunks[0].score == pytest.approx(1.0, abs=1e-5)
    assert chunks[0].metrics == ("average_order_value",)


def test_retrieve_scores_sorted(kb: KnowledgeBase, tmp_path) -> None:
    svc = FakeEmbedding()
    ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb", reset=True)
    r = Retriever(svc, persist_dir=tmp_path, collection_name="test_kb")
    chunks = r.retrieve("average order value payment", k=5)
    scores = [c.score for c in chunks]
    assert scores == sorted(scores, reverse=True)
    assert len(chunks) == 5


def test_retrieve_model_mismatch_raises(kb: KnowledgeBase, tmp_path) -> None:
    ingest(kb, FakeEmbedding(name="model-a"), persist_dir=tmp_path,
           collection_name="test_kb", reset=True)
    with pytest.raises(RetrievalError, match="no silent switch"):
        Retriever(FakeEmbedding(name="model-b"), persist_dir=tmp_path, collection_name="test_kb")


def test_retrieve_missing_collection_raises(tmp_path) -> None:
    with pytest.raises(RetrievalError, match="Could not open"):
        Retriever(FakeEmbedding(), persist_dir=tmp_path, collection_name="nope")


def test_retrieve_empty_question_raises(kb: KnowledgeBase, tmp_path) -> None:
    svc = FakeEmbedding()
    ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb", reset=True)
    r = Retriever(svc, persist_dir=tmp_path, collection_name="test_kb")
    with pytest.raises(RetrievalError, match="empty"):
        r.retrieve("   ", k=3)


def test_retrieve_bad_k_raises(kb: KnowledgeBase, tmp_path) -> None:
    svc = FakeEmbedding()
    ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb", reset=True)
    r = Retriever(svc, persist_dir=tmp_path, collection_name="test_kb")
    with pytest.raises(RetrievalError, match="k must be"):
        r.retrieve("orders", k=0)


def test_retrieved_context_format(kb: KnowledgeBase, tmp_path) -> None:
    svc = FakeEmbedding()
    ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb", reset=True)
    r = Retriever(svc, persist_dir=tmp_path, collection_name="test_kb")
    ctx = retrieved_context(r.retrieve("orders", k=2))
    assert "RETRIEVED KNOWLEDGE" in ctx
    assert "similarity" in ctx


# --- SQL generator integration (additive, Phase 3 path intact) ---------------

VALID_ANSWER = {
    "answerable": True,
    "sql": "SELECT count(*) AS order_count FROM orders",
    "explanation": "Counts all orders.",
    "tables_used": ["orders"],
    "metrics_used": ["order_count"],
    "unsupported_reason": None,
}


def test_generate_without_retriever_has_no_sources(kb: KnowledgeBase) -> None:
    result = generate_sql("How many orders?", FakeLLM(VALID_ANSWER), kb)
    assert result.retrieved_sources == ()


def test_generate_with_retriever_records_sources(kb: KnowledgeBase, tmp_path) -> None:
    svc = FakeEmbedding()
    ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb", reset=True)
    retriever = Retriever(svc, persist_dir=tmp_path, collection_name="test_kb")
    llm = FakeLLM(VALID_ANSWER)
    result = generate_sql("How many orders?", llm, kb, retriever=retriever, k=4)
    assert len(result.retrieved_sources) == 4
    assert all(":" in s for s in result.retrieved_sources)
    # RAG prompt is used (retrieved knowledge header), not the full-KB header.
    assert "RETRIEVED KNOWLEDGE" in llm.last_prompt
    assert "VERIFIED SQL EXAMPLES" not in llm.last_prompt


def test_rag_prompt_contains_question(kb: KnowledgeBase, tmp_path) -> None:
    svc = FakeEmbedding()
    ingest(kb, svc, persist_dir=tmp_path, collection_name="test_kb", reset=True)
    retriever = Retriever(svc, persist_dir=tmp_path, collection_name="test_kb")
    chunks = retriever.retrieve("average order value", k=3)
    prompt = build_rag_prompt("average order value", chunks)
    assert "average order value" in prompt
    assert "USER QUESTION" in prompt
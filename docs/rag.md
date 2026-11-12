# Phase 4 — Retrieval-Augmented Generation (RAG)

## Motivation

Phase 3 grounded SQL generation by putting the **entire** knowledge base (schema
+ glossary + 13 examples) into every prompt. That works but scales poorly: prompt
size grows with the knowledge base, and irrelevant context can distract the model.

Phase 4 embeds the verified knowledge into a persistent vector index and retrieves
only the **most relevant chunks** per question. The core principle is unchanged:
retrieval surfaces *verified* knowledge, and the generated SQL is still
post-validated against the live schema. Retrieval quality is necessary but not
sufficient — Phases 5+ validate and safely execute the SQL.

## Architecture

```
question
   │
   ▼
EmbeddingService.embed_one(question)        ← OpenRouter (or local baseline)
   │  query vector (2048-dim)
   ▼
Retriever.retrieve(question, k)
   │  cosine similarity over ChromaDB collection "olist_knowledge"
   ▼
top-k RetrievedChunk (text + type + source_id + tables + metrics + score)
   │
   ▼
sql_generator.build_rag_prompt(question, chunks)
   │  grounded prompt (retrieved knowledge only)
   ▼
LLMService.generate(...) → JSON → anti-fabrication validation → SQLGenerationResult
                                                                 .retrieved_sources
```

### Components (`app/rag/`)

| Module | Responsibility |
|---|---|
| `embeddings.py` | `EmbeddingService` interface; `OpenRouterEmbeddingService`; optional `LocalSentenceTransformerEmbedding` baseline |
| `embedding_factory.py` | `create_embedding_service(settings)` — the only place that knows the providers |
| `chunking.py` | `build_chunks(kb)` — split the knowledge base into 31 retrievable units |
| `ingestion.py` | `ingest(...)` — embed chunks, persist to ChromaDB, guard against model switches |
| `retriever.py` | `Retriever.retrieve(...)` — read-only semantic search with inspectable, scored sources |

The SQL generator change is **additive**: `generate_sql(question, llm, kb=None,
retriever=None, k=8)`. With `retriever=None` the Phase 3 full-knowledge path is
byte-for-byte unchanged; `SQLGenerationResult` gained a defaulted
`retrieved_sources` field. No Phase 1–3 behavior was modified.

## Embedding provider — verified empirically

The user selected `nvidia/nemotron-3-embed-1b:free` via OpenRouter. Before building
on it, the real API was probed (not assumed):

- The model is **absent from the `/models` catalog**, yet `POST
  https://openrouter.ai/api/v1/embeddings` returns **HTTP 200** — catalog absence
  does not imply unavailability.
- Response shape (OpenAI-compatible):
  `{object, data: [{embedding, index}], model, usage, provider, id}`.
- **Dimension: 2048** (measured, not hard-coded — the service discovers it from the
  first real response).
- **Batch input supported**: a list of texts returns one embedding per text with an
  `index`; the service sorts by `index` to preserve input order.
- Returned `model`: `private/openrouter/nvidia/nemotron-3-embed-1b`; cost 0 (free).

Document and query embedding generation, batch ordering, and dimension discovery
are covered by mocked unit tests (`tests/test_rag.py`) and live tests
(`tests/test_rag_live.py`).

### No silent model switch

- The configured model id is sent **verbatim**; there is no fallback to another
  model on failure — failures raise `EmbeddingError`.
- The collection stores `embedding_model`, `embedding_provider`, and
  `embedding_dimension` in its metadata. Re-ingesting with a different model or
  dimension **raises `IngestionError`** unless `reset=True` is passed, because
  vectors from different models are not comparable.
- The `Retriever` refuses to query an index whose stored model differs from the
  configured embedding model (`RetrievalError`).

### Rate limits and failures

Free endpoints are rate-limited. `OpenRouterEmbeddingService` retries transient
`429`, `5xx`, and transport errors with exponential backoff (2s → 4s → 8s → 16s,
capped), honouring a `Retry-After` header when present. Non-retryable `4xx`
(e.g. 400/401/404) fail fast. All error messages are sanitized — the API key never
appears in logs or exceptions. `trust_env=False` avoids a Windows `NO_PROXY`
parsing crash (same fix as the Phase 3 chat service).

> Observed in practice: after heavy development use, the OpenRouter free daily
> quota was exhausted (`429: free-models-per-day`). The service retried with
> backoff and then raised a clean, sanitized rate-limit error — exactly the
> intended behavior. Live tests are quota-gated and pass when quota is available.

## Chunking strategy

`build_chunks(kb)` produces **31 chunks** from the verified knowledge base:

| Type | Count | Content | Metadata |
|---|---:|---|---|
| `schema` | 7 | one per table: description, PK, FKs, every column | `tables` |
| `metric` | 9 | one per glossary metric: definition, SQL, filters, caveats | `tables`, `metrics` |
| `unsupported_metric` | 2 | net_revenue, profit: why they cannot be computed | `metrics` |
| `example` | 13 | one per verified SQL example: question, SQL, notes | `tables`, `metrics` |

Chunk text carries the natural-language wording a user would use **plus** the
authoritative SQL, so semantic retrieval matches both phrasing and intent. No
dataset results are hard-coded; everything is read from `knowledge_base/`.
ChromaDB metadata values must be scalars, so `tables`/`metrics` tuples are stored
as CSV and parsed back on retrieval.

## Persistence

ChromaDB `PersistentClient` writes the index to `chroma_db/` (gitignored). The
collection is created with `embedding_function=None` and embeddings are always
supplied explicitly, so ChromaDB never downloads its own ONNX model. Cosine space
(`hnsw:space: cosine`) is used for both providers, making the comparison fair.

Persistence was verified by ingesting in one process, then opening a **fresh
process** (new client, no re-ingest) and confirming the chunk count, model
metadata, and correct retrieval (`scripts/verify_rag_persistence.py`). Ingestion
is idempotent for the same model (upsert by chunk id) and prunes stale ids.

## Configuration

```ini
EMBEDDING_PROVIDER=openrouter
OPENROUTER_EMBEDDING_MODEL=nvidia/nemotron-3-embed-1b:free
# API key/base URL fall back to OPENROUTER_API_KEY / OPENROUTER_BASE_URL.
# Optional dedicated overrides:
# OPENROUTER_EMBEDDING_API_KEY=
# OPENROUTER_EMBEDDING_BASE_URL=
```

Optional local baseline (comparison only, never a production fallback):

```ini
EMBEDDING_PROVIDER=local
LOCAL_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
```

Install it with `pip install -r requirements-baseline.txt` (pulls torch).

## Evaluation

`scripts/evaluate_retrieval.py` runs 16 manually verified, **paraphrased** Olist
questions (`knowledge_base/eval/retrieval_questions.json`) against each provider,
ingesting identical chunks into separate eval collections. Metrics: Recall@K,
Hit@K, MRR. Full results: `docs/rag_evaluation.md`.

| Provider | Model | Dim | Recall@1 | Recall@3 | Recall@5 | Hit@3 | MRR |
|---|---|---:|---:|---:|---:|---:|---:|
| OpenRouter | `nvidia/nemotron-3-embed-1b:free` | 2048 | 53.1% | 84.4% | 90.6% | 100% | 0.969 |
| Local baseline | `all-MiniLM-L6-v2` | 384 | 46.9% | 84.4% | 90.6% | 100% | 0.906 |

**Reading the results.** Both providers find at least one relevant chunk in the
top-3 for every question (Hit@3 = 100%). OpenRouter ranks the first relevant chunk
higher more often (Recall@1 53.1% vs 46.9%; MRR 0.969 vs 0.906), so it is the
better primary choice. The three Recall@5 = 50% cases are multi-chunk schema/join
questions where a *secondary* table chunk fell outside the top-5 — the answering
example was still retrieved. The local baseline is a viable offline fallback but
slightly weaker at ranking; it is used only for comparison.

### End-to-end check

`python -m scripts.ask "What is the average order value?" --rag --execute`
retrieved the AOV example + metric, generated the documented SQL, and executed to
**160.99** — exactly matching the Phase 2 verified reference. The RAG prompt used
~1335 input tokens versus the larger full-knowledge Phase 3 prompt.

## Design decisions

1. **Provider-independent embeddings**, mirroring the Phase 3 LLM layer — adding a
   provider means implementing `EmbeddingService` and registering it in the factory.
2. **Discover dimension from the real response**, never hard-code it, so a different
   configured model still works.
3. **Explicit embeddings + `embedding_function=None`** to avoid ChromaDB's ONNX
   download and keep the embedding source fully under our control.
4. **Model metadata guard** to make a silent model switch impossible.
5. **Additive SQL-generator integration** to honor "do not modify Phases 1–3
   unnecessarily"; the full KB is still loaded for anti-fabrication validation.
6. **Inspectable provenance**: every retrieved chunk carries type/source_id/tables/
   metrics/score, surfaced in `retrieved_sources` and the CLI, so grounding is auditable.

## Limitations / next phases

- Retrieval is semantic only (no hybrid keyword/BM25 or reranking yet); a secondary
  table chunk can miss the top-k for broad schema questions.
- The free OpenRouter tier has a daily request quota; heavy use can exhaust it.
- Safe SQL validation/execution (Phase 5), error correction with re-retrieval
  (Phase 6), and a full benchmark (Phase 8) build on this layer.
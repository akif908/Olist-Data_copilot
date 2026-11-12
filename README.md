# Data Analyst Copilot — Verified SQL

An AI-powered data analyst copilot that answers natural-language questions about
the [Olist Brazilian e-commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
with **verified** SQL: generated from retrieved schema knowledge, validated,
safely executed read-only, and explained with grounded results.

**Core principle:** a SQL query that executes successfully is not necessarily
analytically correct. This project emphasizes verified results, not merely SQL
generation.

## Tech stack

Python 3.11 · FastAPI · Streamlit · PostgreSQL 18 · Pandas · OpenRouter
(provider-independent LLM layer; Gemini/Groq pluggable) · ChromaDB · SQLGlot ·
Plotly · Pytest · Docker

## Project status

| Phase | Scope | Status |
|---|---|---|
| 1 | Environment & database foundation | Complete — 32/32 tests passing |
| 2 | Database metadata & business knowledge | Complete — 66/66 tests passing |
| 3 | LLM integration & SQL generation | Complete — 92/92 tests passing |
| 4 | RAG integration | Complete — 123/123 offline tests passing |
| 5 | SQL validation & safe execution | Planned |
| 6 | SQL error handling & correction | Planned |
| 7 | Result analysis & visualization | Planned |
| 8 | Evaluation & benchmarking | Planned |
| 9 | Streamlit frontend | Planned |
| 10 | Testing, deployment & documentation | Planned |

## Setup (Phase 1)

1. **Prerequisites:** Python 3.11, PostgreSQL 18 running, database `olist_copilot` created.
2. **Virtual environment:**

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. **Credentials:** copy `.env.example` to `.env` and set your PostgreSQL
   password and your OpenRouter API key (`OPENROUTER_API_KEY`, from
   <https://openrouter.ai/keys>). `.env` is gitignored — never commit credentials.
   The default model is `cohere/north-mini-code:free`; change `OPENROUTER_MODEL`
   to use another. The LLM layer is provider-independent (`app/llm/`).
   Phase 4 embeddings reuse the same key: `EMBEDDING_PROVIDER=openrouter` and
   `OPENROUTER_EMBEDDING_MODEL=nvidia/nemotron-3-embed-1b:free` (configurable;
   never silently swapped).
4. **Dataset:** download the Olist CSVs from Kaggle and place them in `data/`
   (raw CSVs are gitignored). Expected files: `olist_customers_dataset.csv`,
   `olist_orders_dataset.csv`, `olist_order_items_dataset.csv`,
   `olist_order_payments_dataset.csv`, `olist_order_reviews_dataset.csv`,
   `olist_products_dataset.csv`, `olist_sellers_dataset.csv`,
   `olist_geolocation_dataset.csv`, `product_category_name_translation.csv`.
5. **Inspect the sources** (no database needed):

   ```powershell
   python -m scripts.inspect_data
   ```

6. **Create the schema and import** the 6 Phase 1 tables, with verification:

   ```powershell
   python -m scripts.import_data
   ```

   Refuses to overwrite non-empty tables; use `--replace` to truncate and
   reload (destructive). Writes `docs/import_verification.md`.
7. **Build the knowledge base** (schema metadata + glossary + verified examples):

   ```powershell
   python -m scripts.build_metadata
   python -m scripts.verify_sql_examples
   ```

8. **Run the tests:**

   ```powershell
   python -m pytest
   ```

## Ask a question (Phase 3)

```powershell
python -m scripts.ask "How many orders are in the database?"
python -m scripts.ask "Which five product categories generated the highest revenue?" --execute
python -m scripts.ask --list-models   # free OpenRouter models
```

The generated SQL is **displayed for review, not executed automatically**.
`--execute` is a developer convenience that runs the query in a read-only
transaction with a statement timeout; the full safe executor arrives in Phase 5.
Questions the database cannot answer (e.g. net profit — no cost/refund fields)
are identified as unanswerable with a reason, never guessed.

## Retrieval-Augmented Generation (Phase 4)

Instead of stuffing the entire knowledge base into every prompt, Phase 4 embeds
the verified knowledge (schema, glossary, SQL examples) into a persistent ChromaDB
index and retrieves only the most relevant chunks per question.

```powershell
# 1. Build the persisted vector index (chroma_db/, gitignored)
python -m scripts.ingest_knowledge
python -m scripts.ingest_knowledge --reset          # rebuild / switch models

# 2. Ask with retrieval grounding (prints the retrieved sources)
python -m scripts.ask "What is the average order value?" --rag
python -m scripts.ask "Top five categories by revenue?" --rag --execute

# 3. Verify the index survives a process restart
python -m scripts.verify_rag_persistence

# 4. Evaluate retrieval and compare embedding providers
python -m scripts.evaluate_retrieval                 # OpenRouter vs local baseline
```

- **Embeddings:** `nvidia/nemotron-3-embed-1b:free` via OpenRouter (2048-dim,
  verified empirically). Provider-independent (`app/rag/`); the model is
  configurable and **never silently switched** — a model/dimension mismatch on
  re-ingest raises instead of mixing incomparable vectors.
- **Rate limits:** transient 429/5xx/transport failures are retried with
  exponential backoff (honouring `Retry-After`); errors are sanitized (no key).
- **Optional local baseline:** `pip install -r requirements-baseline.txt` enables
  `sentence-transformers/all-MiniLM-L6-v2` for comparison only — never a
  production fallback.
- **Evaluation:** 16 manually verified, paraphrased Olist questions; Recall@K,
  Hit@K and MRR. See `docs/rag_evaluation.md`.

## Repository structure

```
app/            Application code (config, database, agent, llm, rag)
data/           Raw Olist CSVs (not committed)
db/             SQL DDL and database setup scripts
knowledge_base/ Schema docs, business glossary, verified SQL examples, eval set
scripts/        Data inspection, import, knowledge-base, RAG and eval utilities
docs/           Inspection report, schema, metadata, glossary, RAG, architecture
tests/          Pytest suite
chroma_db/      Persisted RAG vector index (generated, not committed)
```

## Documentation

- `docs/data_inspection.md` — profiling of the source CSVs (Phase 1)
- `docs/schema.md` — relational schema, ER diagram and design decisions
- `docs/import_verification.md` — row counts, FK checks and column types
- `docs/metadata.md` — how schema metadata and business knowledge are built (Phase 2)
- `docs/business_glossary.md` — metric definitions with caveats (Phase 2)
- `docs/sql_examples.md` — verified SQL example library with real outputs (Phase 2)
- `docs/rag.md` — RAG architecture, embedding providers and design decisions (Phase 4)
- `docs/rag_evaluation.md` — retrieval evaluation and provider comparison (Phase 4)
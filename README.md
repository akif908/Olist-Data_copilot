# Data Analyst Copilot — Verified SQL

An AI-powered data analyst copilot that answers natural-language questions about
the [Olist Brazilian e-commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
with **verified** SQL: generated from retrieved schema knowledge, validated,
safely executed read-only, and explained with grounded results.

**Core principle:** a SQL query that executes successfully is not necessarily
analytically correct. This project emphasizes verified results, not merely SQL
generation.

## Tech stack

Python 3.11 · FastAPI · Streamlit · PostgreSQL 18 · Pandas · Google Gemini
(provider-independent LLM layer) · ChromaDB · SQLGlot · Plotly · Pytest · Docker

## Project status

| Phase | Scope | Status |
|---|---|---|
| 1 | Environment & database foundation | Complete — 32/32 tests passing |
| 2 | Database metadata & business knowledge | Complete — 66/66 tests passing |
| 3 | LLM integration & SQL generation | Planned |
| 4 | RAG integration | Planned |
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

3. **Credentials:** copy `.env.example` to `.env` and set your PostgreSQL password.
   `.env` is gitignored — never commit credentials.
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

## Repository structure

```
app/            Application code (config, database, later: agent, llm, rag, ...)
data/           Raw Olist CSVs (not committed)
db/             SQL DDL and database setup scripts
knowledge_base/ Schema docs, business glossary, verified SQL examples
scripts/        Data inspection, import and knowledge-base utilities
docs/           Inspection report, schema, metadata, glossary, architecture
tests/          Pytest suite
```

## Documentation

- `docs/data_inspection.md` — profiling of the source CSVs (Phase 1)
- `docs/schema.md` — relational schema, ER diagram and design decisions
- `docs/import_verification.md` — row counts, FK checks and column types
- `docs/metadata.md` — how schema metadata and business knowledge are built (Phase 2)
- `docs/business_glossary.md` — metric definitions with caveats (Phase 2)
- `docs/sql_examples.md` — verified SQL example library with real outputs (Phase 2)
# Metadata & Business Knowledge (Phase 2)

How the application "understands" the database. Everything here is grounded in
the live database or in explicitly verified facts — no fabricated schema
information reaches the LLM.

## Components and data flow

```
PostgreSQL (live)                 Curated knowledge (human-written)
─────────────────                 ────────────────────────────────
schema_inspector.py               table_descriptions.json
  tables, columns, types,           plain-language description
  nullability, defaults,            for every table and column
  PKs, FKs, indexes,
  comments, row counts
        │                                   │
        └──────────────┬────────────────────┘
                       ▼
              metadata.py (merge + validate)
              • FabricatedMetadataError if a documented
                table/column does not exist in the DB
              • MissingDescriptionError if any live
                table/column lacks a description
                       ▼
        knowledge_base/schema_docs/schema_metadata.json
          (single source of schema truth for
           Phase 3 SQL generation and Phase 4 RAG)

knowledge_base/business_glossary/glossary.json
  9 metrics: definition, canonical SQL, unit, filters, caveats
  + unsupported metrics (net_revenue, profit) with reasons
                       ▼
        rendered to docs/business_glossary.md

knowledge_base/sql_examples/examples.json
  13 hand-written question → SQL examples
                       ▼
        scripts/verify_sql_examples.py executes each in a
        READ ONLY transaction and records the real output
                       ▼
        verified_results.json + docs/sql_examples.md
```

## Modules

| File | Responsibility |
|---|---|
| `app/database/schema_inspector.py` | Live introspection via `pg_catalog` (precise types like `numeric(10,2)`, composite key order preserved, comments included). Returns typed dataclasses. |
| `app/database/metadata.py` | Loads curated descriptions, validates them against the live schema in both directions, merges into one JSON artifact. |
| `knowledge_base/schema_docs/table_descriptions.json` | Curated descriptions (43 columns, 7 tables). Edit this file to improve wording — validation keeps it honest. |
| `knowledge_base/business_glossary/glossary.json` | Metric definitions. Each metric references only real tables/columns and links to a verified SQL example. |
| `knowledge_base/sql_examples/examples.json` | Question/SQL pairs with notes on metric interpretation. |
| `scripts/build_metadata.py` | Rebuilds `schema_metadata.json` and renders `docs/business_glossary.md`. |
| `scripts/verify_sql_examples.py` | Re-verifies every example and refreshes `verified_results.json` + `docs/sql_examples.md`. |

## Validation rules (enforced by tests)

1. **No fabrication** — a documented table or column that does not exist in
   the live database raises `FabricatedMetadataError`.
2. **Full coverage** — every live table and column must have a description,
   else `MissingDescriptionError`.
3. **Glossary integrity** — all 9 required metrics present; every referenced
   table/column exists; every metric has caveats; each metric's SQL equals
   its linked example's SQL.
4. **Examples are verified, not asserted** — each example executes in a
   read-only transaction and its recorded output must match on re-execution
   (row counts and scalar values).

## Rebuilding after changes

```powershell
python -m scripts.build_metadata         # schema changes or description edits
python -m scripts.verify_sql_examples    # after data or example changes
python -m pytest                         # re-run all validations
```

## Key business definitions (summary)

Full definitions with caveats: [business_glossary.md](business_glossary.md).

- **Customer count** uses `customer_unique_id` (96,096 people), never
  `customer_id` (99,441 order-side records).
- **AOV** is payment-based, includes freight, excludes the one payment-less
  order, and includes payments of canceled orders (all 625 have payment rows).
- **Gross item value** = `SUM(price)`, merchandise only. It is **not net
  revenue** — refunds are not observable in this dataset.
- **Cancellation rate** counts only `canceled` (625), not `unavailable` (609).
- **Delivery time** is purchase-to-door in days (12.56 avg), excluding orders
  without a delivery timestamp.
- **Review score** averages review rows (4.09), not orders; `review_id`
  repeats across orders.
"""Build the knowledge-base artifacts from the live database.

1. Inspect the live schema, merge curated descriptions, validate them
   (no fabrication, full coverage) -> schema_metadata.json.
2. Render the business glossary to docs/business_glossary.md.

SQL examples are verified separately: python -m scripts.verify_sql_examples.

Usage:
    python -m scripts.build_metadata
"""
from __future__ import annotations

import json

from app.config import PROJECT_ROOT
from app.database.connection import get_connection
from app.database.metadata import build_metadata, write_metadata

GLOSSARY_PATH = PROJECT_ROOT / "knowledge_base" / "business_glossary" / "glossary.json"
GLOSSARY_DOC_PATH = PROJECT_ROOT / "docs" / "business_glossary.md"


def render_glossary(glossary: dict) -> str:
    lines = [
        "# Business Glossary — Olist Copilot",
        "",
        f"Currency: {glossary['currency']}. {glossary['notes']}",
        "",
        "Source of truth: `knowledge_base/business_glossary/glossary.json`.",
        "Rendered by `python -m scripts.build_metadata`.",
        "",
    ]
    for m in glossary["metrics"]:
        lines += [
            f"## {m['name']} (`{m['id']}`)",
            "",
            m["definition"],
            "",
            f"```sql\n{m['sql']}\n```",
            "",
            f"- **Unit:** {m['unit']}",
            f"- **Tables:** {', '.join(m['tables'])}",
            f"- **Filters:** {m['filters']}",
            "- **Caveats:**",
        ]
        lines += [f"  - {c}" for c in m["caveats"]]
        lines.append("")
    lines += ["## Metrics the dataset cannot support", ""]
    for m in glossary["unsupported_metrics"]:
        lines += [f"- **{m['id']}**: {m['reason']}"]
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    with get_connection() as conn:
        metadata = build_metadata(conn)
    write_metadata(metadata)

    n_tables = len(metadata["tables"])
    n_columns = sum(len(t["columns"]) for t in metadata["tables"].values())
    n_fks = sum(len(t["foreign_keys"]) for t in metadata["tables"].values())
    print(f"schema_metadata.json written: {n_tables} tables, {n_columns} columns, {n_fks} FKs")
    for name, t in metadata["tables"].items():
        print(f"  {name}: {len(t['columns'])} columns, {t['row_count']} rows, "
              f"PK={t['primary_key']}")

    glossary = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    GLOSSARY_DOC_PATH.write_text(render_glossary(glossary), encoding="utf-8")
    print(f"business_glossary.md rendered: {len(glossary['metrics'])} metrics, "
          f"{len(glossary['unsupported_metrics'])} unsupported")


if __name__ == "__main__":
    main()
"""Chunk the verified knowledge base into retrievable units.

Each chunk is a self-contained piece of knowledge that can be embedded and
retrieved independently, with metadata that makes the source inspectable:

* schema   — one per table (description, keys, columns)        -> 7 chunks
* metric   — one per glossary metric (definition, SQL, caveats) -> 9 chunks
* unsupported_metric — one per unsupported metric (reason)      -> 2 chunks
* example  — one per verified SQL example (question, SQL, notes)-> 13 chunks

Chunk text is written for semantic retrieval: it carries the natural-language
question/definition wording a user would use, plus the authoritative SQL. No
dataset results are hard-coded here; everything is read from the knowledge base.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agent.sql_generator import KnowledgeBase

SCHEMA = "schema"
METRIC = "metric"
UNSUPPORTED_METRIC = "unsupported_metric"
EXAMPLE = "example"


@dataclass(frozen=True)
class Chunk:
    """A retrievable knowledge unit. ``tables``/``metrics`` aid filtering."""

    id: str
    text: str
    type: str
    source_id: str
    tables: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, str]:
        # ChromaDB metadata values must be scalars; encode tuples as CSV.
        return {
            "type": self.type,
            "source_id": self.source_id,
            "tables": ",".join(self.tables),
            "metrics": ",".join(self.metrics),
        }


def _schema_chunk(name: str, t: dict) -> Chunk:
    lines = [f"TABLE {name} ({t['row_count']} rows): {t['description']}"]
    lines.append(f"PRIMARY KEY ({', '.join(t['primary_key'])})")
    for fk in t["foreign_keys"]:
        lines.append(
            f"FOREIGN KEY ({', '.join(fk['columns'])}) REFERENCES "
            f"{fk['referenced_table']} ({', '.join(fk['referenced_columns'])})"
        )
    lines.append("COLUMNS:")
    for cname, c in t["columns"].items():
        null = "NULL" if c["is_nullable"] else "NOT NULL"
        lines.append(f"- {cname} {c['data_type']} {null}: {c['description']}")
    return Chunk(
        id=f"schema:{name}",
        text="\n".join(lines),
        type=SCHEMA,
        source_id=name,
        tables=(name,),
    )


def _metric_chunk(m: dict) -> Chunk:
    lines = [
        f"METRIC {m['id']} — {m['name']}",
        f"Definition: {m['definition']}",
        f"SQL: {m['sql']}",
        f"Unit: {m['unit']}",
        f"Tables: {', '.join(m['tables'])}",
        f"Filters: {m['filters']}",
    ]
    if m.get("caveats"):
        lines.append("Caveats:")
        lines.extend(f"- {c}" for c in m["caveats"])
    return Chunk(
        id=f"metric:{m['id']}",
        text="\n".join(lines),
        type=METRIC,
        source_id=m["id"],
        tables=tuple(m["tables"]),
        metrics=(m["id"],),
    )


def _unsupported_chunk(m: dict) -> Chunk:
    text = (
        f"UNSUPPORTED METRIC {m['id']} (must not be computed)\n"
        f"Reason: {m['reason']}"
    )
    return Chunk(
        id=f"unsupported_metric:{m['id']}",
        text=text,
        type=UNSUPPORTED_METRIC,
        source_id=m["id"],
        metrics=(m["id"],),
    )


def _example_chunk(ex: dict) -> Chunk:
    lines = [
        f"EXAMPLE {ex['id']}",
        f"Question: {ex['question']}",
        f"SQL: {ex['sql']}",
    ]
    if ex.get("notes"):
        lines.append(f"Notes: {ex['notes']}")
    return Chunk(
        id=f"example:{ex['id']}",
        text="\n".join(lines),
        type=EXAMPLE,
        source_id=ex["id"],
        tables=tuple(ex.get("tables", [])),
        metrics=tuple(ex.get("metrics", [])),
    )


def build_chunks(kb: KnowledgeBase) -> list[Chunk]:
    """Build all knowledge chunks in a stable order (schema, metric, example)."""
    chunks: list[Chunk] = []
    for name, t in kb.metadata["tables"].items():
        chunks.append(_schema_chunk(name, t))
    for m in kb.glossary["metrics"]:
        chunks.append(_metric_chunk(m))
    for m in kb.glossary.get("unsupported_metrics", []):
        chunks.append(_unsupported_chunk(m))
    for ex in kb.examples:
        chunks.append(_example_chunk(ex))
    return chunks
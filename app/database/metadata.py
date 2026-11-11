"""Combine live schema metadata with curated business descriptions.

The curated file (knowledge_base/schema_docs/table_descriptions.json) adds a
plain-language description for every table and column. This module merges it
with the live inspection result and enforces two rules:

1. No fabrication: every documented table/column must exist in the database.
2. Full coverage: every live table/column must have a description.

The combined artifact (schema_metadata.json) is the single source of schema
truth for SQL generation (Phase 3) and RAG ingestion (Phase 4).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from app.config import PROJECT_ROOT
from app.database.schema_inspector import TableInfo, inspect_schema

DESCRIPTIONS_PATH = (
    PROJECT_ROOT / "knowledge_base" / "schema_docs" / "table_descriptions.json"
)
OUTPUT_PATH = (
    PROJECT_ROOT / "knowledge_base" / "schema_docs" / "schema_metadata.json"
)


class MetadataError(Exception):
    """Base class for metadata validation failures."""


class FabricatedMetadataError(MetadataError):
    """A documented table or column does not exist in the live database."""


class MissingDescriptionError(MetadataError):
    """A live table or column has no curated description."""


def load_descriptions(path: Path = DESCRIPTIONS_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["tables"]


def validate_descriptions(
    schema: dict[str, TableInfo], descriptions: dict
) -> None:
    """Raise on fabricated or missing descriptions (both directions)."""
    for tname, tdoc in descriptions.items():
        if tname not in schema:
            raise FabricatedMetadataError(f"documented table does not exist: {tname}")
        live_columns = set(schema[tname].column_names())
        for cname in tdoc.get("columns", {}):
            if cname not in live_columns:
                raise FabricatedMetadataError(
                    f"documented column does not exist: {tname}.{cname}"
                )

    for tname, table in schema.items():
        tdoc = descriptions.get(tname)
        if not tdoc or not tdoc.get("description"):
            raise MissingDescriptionError(f"missing table description: {tname}")
        for cname in table.column_names():
            if not tdoc.get("columns", {}).get(cname):
                raise MissingDescriptionError(f"missing column description: {tname}.{cname}")


def build_metadata(
    conn: psycopg.Connection, descriptions_path: Path = DESCRIPTIONS_PATH
) -> dict:
    """Inspect the live schema, validate curated descriptions, merge them."""
    schema = inspect_schema(conn)
    descriptions = load_descriptions(descriptions_path)
    validate_descriptions(schema, descriptions)

    tables: dict[str, dict] = {}
    for tname, table in schema.items():
        tdoc = descriptions[tname]
        tables[tname] = {
            "description": tdoc["description"],
            "db_comment": table.comment,
            "row_count": table.row_count,
            "primary_key": list(table.primary_key),
            "foreign_keys": [
                {
                    "columns": list(fk.columns),
                    "referenced_table": fk.referenced_table,
                    "referenced_columns": list(fk.referenced_columns),
                }
                for fk in table.foreign_keys
            ],
            "indexes": list(table.indexes),
            "columns": {
                c.name: {
                    "data_type": c.data_type,
                    "is_nullable": c.is_nullable,
                    "column_default": c.column_default,
                    "db_comment": c.comment,
                    "description": tdoc["columns"][c.name],
                }
                for c in table.columns
            },
        }

    return {
        "database": conn.execute("SELECT current_database()").fetchone()[0],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }


def write_metadata(metadata: dict, path: Path = OUTPUT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
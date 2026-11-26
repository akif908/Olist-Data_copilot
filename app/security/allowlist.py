"""Approved schema/table/column allowlist, derived from verified metadata.

The single source of truth is the Phase 2 live-inspected schema metadata
(``knowledge_base/schema_docs/schema_metadata.json``) — the same anti-fabrication
metadata used by the SQL generator. Nothing here is hard-coded or inferred from
the SQL being validated.

Only the ``public`` schema and the seven verified Olist tables are approved.
System catalogs (``pg_catalog``, ``information_schema``) and any other schema are
denied. The database role (Phase 5 L3) independently grants SELECT on exactly
these tables, so this allowlist is a redundant early-rejection layer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT

METADATA_PATH = PROJECT_ROOT / "knowledge_base" / "schema_docs" / "schema_metadata.json"
APPROVED_SCHEMAS = frozenset({"", "public"})


@dataclass(frozen=True)
class SchemaAllowlist:
    tables: frozenset[str]
    columns: dict[str, frozenset[str]]
    allowed_schemas: frozenset[str] = APPROVED_SCHEMAS

    @classmethod
    def from_metadata(cls, metadata: dict) -> SchemaAllowlist:
        tables = frozenset(metadata["tables"])
        columns = {
            name: frozenset(t["columns"]) for name, t in metadata["tables"].items()
        }
        return cls(tables=tables, columns=columns)

    @classmethod
    def load(cls, path: Path = METADATA_PATH) -> SchemaAllowlist:
        return cls.from_metadata(json.loads(path.read_text("utf-8")))

    @property
    def all_columns(self) -> frozenset[str]:
        out: set[str] = set()
        for cols in self.columns.values():
            out |= cols
        return frozenset(out)

    def is_table_allowed(self, name: str) -> bool:
        return name in self.tables

    def is_column_allowed(self, table: str, column: str) -> bool:
        return column in self.columns.get(table, frozenset())

    def sqlglot_schema(self) -> dict[str, dict[str, str]]:
        """Schema mapping for SQLGlot qualification (types are not security-relevant)."""
        return {t: {c: "text" for c in cols} for t, cols in self.columns.items()}
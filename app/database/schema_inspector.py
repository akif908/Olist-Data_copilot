"""Inspect live PostgreSQL schema metadata.

Reads tables, columns, precise types, nullability, defaults, table/column
comments, primary keys, foreign keys, indexes and row counts from the
pg_catalog / information_schema of the connected database.

Everything comes from the live database — this module never invents schema
information. Curated business descriptions are merged separately in
app.database.metadata.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import psycopg

_TABLES_QUERY = """
SELECT c.relname AS table_name,
       obj_description(c.oid, 'pg_class') AS table_comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relname
"""

_COLUMNS_QUERY = """
SELECT c.relname AS table_name,
       a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull AS is_nullable,
       pg_get_expr(ad.adbin, ad.adrelid) AS column_default,
       col_description(a.attrelid, a.attnum) AS column_comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

_CONSTRAINTS_QUERY = """
SELECT c.relname AS table_name,
       con.conname AS constraint_name,
       con.contype AS constraint_type,
       ARRAY(
           SELECT att.attname
           FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN pg_attribute att
             ON att.attrelid = con.conrelid AND att.attnum = k.attnum
           ORDER BY k.ord
       ) AS columns,
       frel.relname AS referenced_table,
       ARRAY(
           SELECT att.attname
           FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN pg_attribute att
             ON att.attrelid = con.confrelid AND att.attnum = k.attnum
           ORDER BY k.ord
       ) AS referenced_columns
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class frel ON frel.oid = con.confrelid
WHERE n.nspname = 'public' AND con.contype IN ('p', 'f')
ORDER BY c.relname, con.conname
"""

_INDEXES_QUERY = """
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname
"""


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    is_nullable: bool
    column_default: str | None
    comment: str | None


@dataclass(frozen=True)
class ForeignKeyInfo:
    name: str
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableInfo:
    name: str
    comment: str | None
    columns: tuple[ColumnInfo, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]
    indexes: tuple[str, ...]
    row_count: int

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def to_dict(self) -> dict:
        return asdict(self)


def inspect_schema(conn: psycopg.Connection) -> dict[str, TableInfo]:
    """Return full live metadata for every base table in schema public."""
    tables: dict[str, dict] = {}
    for name, comment in conn.execute(_TABLES_QUERY).fetchall():
        tables[name] = {
            "comment": comment,
            "columns": [],
            "primary_key": (),
            "foreign_keys": [],
            "indexes": [],
        }

    for tname, cname, dtype, nullable, default, ccomment in conn.execute(
        _COLUMNS_QUERY
    ).fetchall():
        tables[tname]["columns"].append(
            ColumnInfo(cname, dtype, nullable, default, ccomment)
        )

    for tname, cname, ctype, cols, reftable, refcols in conn.execute(
        _CONSTRAINTS_QUERY
    ).fetchall():
        if ctype == "p":
            tables[tname]["primary_key"] = tuple(cols)
        elif ctype == "f":
            tables[tname]["foreign_keys"].append(
                ForeignKeyInfo(cname, tuple(cols), reftable, tuple(refcols))
            )

    for tname, _iname, idef in conn.execute(_INDEXES_QUERY).fetchall():
        tables[tname]["indexes"].append(idef)

    result: dict[str, TableInfo] = {}
    for name, t in tables.items():
        row_count = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        result[name] = TableInfo(
            name=name,
            comment=t["comment"],
            columns=tuple(t["columns"]),
            primary_key=t["primary_key"],
            foreign_keys=tuple(t["foreign_keys"]),
            indexes=tuple(t["indexes"]),
            row_count=row_count,
        )
    return result
"""Phase 2 tests: schema inspector, metadata validation, glossary integrity.

Requires the imported database (Phase 1 + order_reviews).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import psycopg
import pytest

from app.config import PROJECT_ROOT
from app.database.connection import get_connection
from app.database.metadata import (
    FabricatedMetadataError,
    MissingDescriptionError,
    build_metadata,
    load_descriptions,
)
from app.database.schema_inspector import inspect_schema

GLOSSARY_PATH = PROJECT_ROOT / "knowledge_base" / "business_glossary" / "glossary.json"
EXAMPLES_PATH = PROJECT_ROOT / "knowledge_base" / "sql_examples" / "examples.json"

EXPECTED_TABLES = {
    "customers", "orders", "products", "product_category_translation",
    "order_items", "order_payments", "order_reviews",
}
REQUIRED_METRICS = {
    "order_count", "customer_count", "average_order_value", "gross_item_value",
    "freight_value", "delivered_orders", "cancellation_rate",
    "average_delivery_time", "review_score",
}


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    with get_connection() as c:
        yield c


@pytest.fixture(scope="module")
def schema(conn: psycopg.Connection) -> dict:
    return inspect_schema(conn)


@pytest.fixture(scope="module")
def glossary() -> dict:
    return json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def examples() -> list[dict]:
    return json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))["examples"]


# --- Schema inspector -------------------------------------------------------

def test_inspector_finds_all_tables(schema: dict) -> None:
    assert set(schema) == EXPECTED_TABLES


def test_inspector_row_counts_positive(schema: dict) -> None:
    assert all(t.row_count > 0 for t in schema.values())


def test_inspector_detects_composite_pks(schema: dict) -> None:
    assert schema["order_items"].primary_key == ("order_id", "order_item_id")
    assert schema["order_payments"].primary_key == ("order_id", "payment_sequential")
    assert schema["order_reviews"].primary_key == ("review_id", "order_id")


def test_inspector_detects_foreign_keys(schema: dict) -> None:
    fks = {
        (t.name, fk.columns[0], fk.referenced_table)
        for t in schema.values() for fk in t.foreign_keys
    }
    assert fks == {
        ("orders", "customer_id", "customers"),
        ("order_items", "order_id", "orders"),
        ("order_items", "product_id", "products"),
        ("order_payments", "order_id", "orders"),
        ("order_reviews", "order_id", "orders"),
    }


def test_inspector_reads_comments(schema: dict) -> None:
    assert all(t.comment for t in schema.values())


def test_inspector_precise_types(schema: dict) -> None:
    cols = {c.name: c for c in schema["order_items"].columns}
    assert cols["price"].data_type == "numeric(10,2)"
    assert cols["order_id"].data_type == "character varying(32)"
    assert cols["shipping_limit_date"].data_type == "timestamp without time zone"
    assert cols["price"].is_nullable is False
    prod = {c.name: c for c in schema["products"].columns}
    assert prod["product_category_name"].is_nullable is True


# --- Metadata merge & validation --------------------------------------------

def test_build_metadata_full_coverage(conn: psycopg.Connection) -> None:
    meta = build_metadata(conn)
    assert set(meta["tables"]) == EXPECTED_TABLES
    for t in meta["tables"].values():
        assert t["description"]
        for c in t["columns"].values():
            assert c["description"]
            assert c["data_type"]


def test_fabricated_table_rejected(conn: psycopg.Connection, tmp_path: Path) -> None:
    descriptions = load_descriptions()
    descriptions["ghost_table"] = {"description": "x", "columns": {}}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"tables": descriptions}), encoding="utf-8")
    with pytest.raises(FabricatedMetadataError, match="ghost_table"):
        build_metadata(conn, p)


def test_fabricated_column_rejected(conn: psycopg.Connection, tmp_path: Path) -> None:
    descriptions = load_descriptions()
    descriptions["orders"]["columns"]["ghost_column"] = "x"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"tables": descriptions}), encoding="utf-8")
    with pytest.raises(FabricatedMetadataError, match="ghost_column"):
        build_metadata(conn, p)


def test_missing_description_rejected(conn: psycopg.Connection, tmp_path: Path) -> None:
    descriptions = load_descriptions()
    del descriptions["orders"]["columns"]["order_status"]
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"tables": descriptions}), encoding="utf-8")
    with pytest.raises(MissingDescriptionError, match="order_status"):
        build_metadata(conn, p)


# --- Business glossary -------------------------------------------------------

def test_glossary_has_all_required_metrics(glossary: dict) -> None:
    assert {m["id"] for m in glossary["metrics"]} == REQUIRED_METRICS


def test_glossary_references_real_schema(glossary: dict, schema: dict) -> None:
    for m in glossary["metrics"]:
        for t in m["tables"]:
            assert t in schema, f"{m['id']}: unknown table {t}"
        for qualified in m["columns"]:
            tname, cname = qualified.split(".")
            assert tname in schema, f"{m['id']}: unknown table {tname}"
            assert cname in schema[tname].column_names(), (
                f"{m['id']}: unknown column {qualified}"
            )


def test_glossary_metrics_have_caveats(glossary: dict) -> None:
    assert all(m["caveats"] for m in glossary["metrics"])


def test_glossary_links_match_examples(glossary: dict, examples: list[dict]) -> None:
    by_id = {e["id"]: e for e in examples}
    for m in glossary["metrics"]:
        ex = by_id[m["example_id"]]
        assert ex["sql"] == m["sql"], f"{m['id']}: SQL differs from its example"
        assert m["id"] in ex["metrics"]


def test_unsupported_metrics_documented(glossary: dict) -> None:
    unsupported = {m["id"] for m in glossary["unsupported_metrics"]}
    assert {"net_revenue", "profit"} <= unsupported
    assert all(m["reason"] for m in glossary["unsupported_metrics"])
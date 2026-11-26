"""Phase 5 Step 1 — offline unit tests for the SQLGlot AST validator (L2).

No database connection is required. These tests assert that:
* all 13 verified Phase 2 SQL examples pass;
* extra valid structures (CTE, subquery, UNION, alias, public-qualified,
  ORDER BY/LIMIT/OFFSET, mixed-case unquoted identifiers) pass;
* every adversarial category is rejected with its specific code;
* the ORIGINAL SQL text is returned unchanged (no rewriting, refinement #4);
* the allowlist is derived from the verified metadata, not hard-coded.
"""
from __future__ import annotations

import json

import pytest

from app.config import PROJECT_ROOT
from app.security.allowlist import SchemaAllowlist
from app.security.sql_validator import (
    SQLValidationError,
    ValidationResult,
    validate_sql,
)
from tests.security_payloads import ATTACKS, VALID_QUERIES

EXAMPLES = json.loads(
    (PROJECT_ROOT / "knowledge_base" / "sql_examples" / "examples.json").read_text("utf-8")
)["examples"]


@pytest.fixture(scope="module")
def allowlist() -> SchemaAllowlist:
    return SchemaAllowlist.load()


# --- allowlist provenance ---------------------------------------------------

def test_allowlist_matches_verified_metadata():
    meta = json.loads(
        (PROJECT_ROOT / "knowledge_base" / "schema_docs" / "schema_metadata.json").read_text("utf-8")
    )
    al = SchemaAllowlist.load()
    assert al.tables == frozenset(meta["tables"])
    assert len(al.tables) == 7
    for name, t in meta["tables"].items():
        assert al.columns[name] == frozenset(t["columns"])


def test_allowlist_denies_catalogs_and_unknown():
    al = SchemaAllowlist.load()
    assert "public" in al.allowed_schemas and "" in al.allowed_schemas
    assert "pg_catalog" not in al.allowed_schemas
    assert "information_schema" not in al.allowed_schemas
    assert not al.is_table_allowed("sellers")
    assert not al.is_column_allowed("orders", "pwned")


# --- verified examples and valid structures ---------------------------------

@pytest.mark.parametrize("example", EXAMPLES, ids=[e["id"] for e in EXAMPLES])
def test_verified_examples_pass(example, allowlist):
    result = validate_sql(example["sql"], allowlist)
    assert isinstance(result, ValidationResult)
    # every real table the example declares is reported
    assert set(example["tables"]).issubset(set(result.tables))


@pytest.mark.parametrize("sql", VALID_QUERIES)
def test_valid_queries_pass(sql, allowlist):
    result = validate_sql(sql, allowlist)
    assert result.tables  # at least one real approved table


def test_no_rewriting_original_sql_preserved(allowlist):
    sql = "SELECT product_id, price FROM order_items ORDER BY price DESC LIMIT 10 OFFSET 5"
    result = validate_sql(sql, allowlist)
    assert result.sql == sql  # exact original, including LIMIT/OFFSET/ORDER BY


def test_cte_underlying_tables_reported(allowlist):
    sql = (
        "WITH rev AS (SELECT order_id, avg(review_score) AS s FROM order_reviews GROUP BY order_id) "
        "SELECT o.order_status FROM orders o JOIN rev r ON o.order_id = r.order_id"
    )
    result = validate_sql(sql, allowlist)
    assert "orders" in result.tables and "order_reviews" in result.tables
    assert "rev" not in result.tables  # CTE name is not a real table


def test_quoted_uppercase_table_rejected(allowlist):
    # PostgreSQL treats "ORDERS" as a distinct, non-existent relation.
    with pytest.raises(SQLValidationError) as ei:
        validate_sql('SELECT * FROM "ORDERS"', allowlist)
    assert ei.value.code == "unknown_table"


def test_public_qualified_passes(allowlist):
    result = validate_sql("SELECT count(*) FROM public.orders", allowlist)
    assert result.tables == ("orders",)


# --- adversarial corpus -----------------------------------------------------

@pytest.mark.parametrize(
    "category,sql,expected_code",
    [(cat, sql, code) for cat, items in ATTACKS.items() for sql, code in items],
    ids=[f"{cat}-{i}" for cat, items in ATTACKS.items() for i in range(len(items))],
)
def test_attacks_rejected_with_code(category, sql, expected_code, allowlist):
    with pytest.raises(SQLValidationError) as ei:
        validate_sql(sql, allowlist)
    assert ei.value.code == expected_code, (
        f"{category}: expected {expected_code}, got {ei.value.code} for {sql!r}"
    )


def test_every_attack_category_present():
    # guard against an accidentally emptied corpus
    assert set(ATTACKS) >= {
        "dml", "ddl", "multi_statement", "catalog", "dangerous_function",
        "table_function", "select_into", "row_lock", "data_modifying_cte",
        "unknown_schema_object", "obfuscated", "empty",
    }
    assert sum(len(v) for v in ATTACKS.values()) >= 40
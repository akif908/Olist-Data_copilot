"""Phase 1 tests: connectivity, schema, row counts, keys, relationships.

Requires PostgreSQL running, .env configured, and data imported
(`python -m scripts.import_data`). Expected values are derived from the
source CSVs at runtime — no dataset results are hard-coded.
"""
from __future__ import annotations

from typing import Iterator

import pandas as pd
import psycopg
import pytest

from app.config import DATA_DIR
from app.database.connection import get_connection
from scripts.import_data import FK_CHECKS, TABLE_LOADS

# table -> primary-key columns (as designed in db/schema.sql)
PRIMARY_KEYS: dict[str, list[str]] = {
    "customers": ["customer_id"],
    "orders": ["order_id"],
    "product_category_translation": ["product_category_name"],
    "products": ["product_id"],
    "order_items": ["order_id", "order_item_id"],
    "order_payments": ["order_id", "payment_sequential"],
    "order_reviews": ["review_id", "order_id"],
}

MONEY_COLUMNS = [
    ("order_items", "price"),
    ("order_items", "freight_value"),
    ("order_payments", "payment_value"),
]


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    with get_connection() as c:
        yield c


def test_connection(conn: psycopg.Connection) -> None:
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert conn.execute("SELECT current_database()").fetchone()[0] == "olist_copilot"


def test_all_tables_exist(conn: psycopg.Connection) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    existing = {r[0] for r in rows}
    for table, _ in TABLE_LOADS:
        assert table in existing, f"missing table {table}"


@pytest.mark.parametrize("table,filename", TABLE_LOADS)
def test_row_count_matches_source(
    conn: psycopg.Connection, table: str, filename: str
) -> None:
    db_n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    csv_n = len(pd.read_csv(DATA_DIR / filename, usecols=[0]))
    assert db_n == csv_n, f"{table}: DB {db_n} != CSV {csv_n}"


@pytest.mark.parametrize("table,cols", PRIMARY_KEYS.items())
def test_primary_key_constraint_exists(
    conn: psycopg.Connection, table: str, cols: list[str]
) -> None:
    rows = conn.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = 'public' AND tc.table_name = %s
        ORDER BY kcu.ordinal_position
        """,
        (table,),
    ).fetchall()
    assert [r[0] for r in rows] == cols


@pytest.mark.parametrize("table,cols", PRIMARY_KEYS.items())
def test_no_duplicate_keys(conn: psycopg.Connection, table: str, cols: list[str]) -> None:
    col_list = ", ".join(cols)
    n = conn.execute(
        f"SELECT count(*) FROM (SELECT {col_list} FROM {table} "
        f"GROUP BY {col_list} HAVING count(*) > 1) d"
    ).fetchone()[0]
    assert n == 0, f"duplicate key values in {table}"


def test_enforced_foreign_keys_exist(conn: psycopg.Connection) -> None:
    rows = conn.execute(
        """
        SELECT tc.table_name, kcu.column_name, ccu.table_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
        """
    ).fetchall()
    actual = {(r[0], r[1], r[2]) for r in rows}
    for child, ccols, parent, _ in FK_CHECKS:
        assert (child, ccols[0], parent) in actual


@pytest.mark.parametrize("child,ccols,parent,pcols", FK_CHECKS)
def test_no_fk_orphans(
    conn: psycopg.Connection,
    child: str, ccols: list[str], parent: str, pcols: list[str],
) -> None:
    join = " AND ".join(f"c.{a} = p.{b}" for a, b in zip(ccols, pcols))
    n = conn.execute(
        f"SELECT count(*) FROM {child} c LEFT JOIN {parent} p ON {join} "
        f"WHERE c.{ccols[0]} IS NOT NULL AND p.{pcols[0]} IS NULL"
    ).fetchone()[0]
    assert n == 0, f"{child} -> {parent}: {n} orphans"


@pytest.mark.parametrize("table,column", MONEY_COLUMNS)
def test_money_columns_are_numeric(
    conn: psycopg.Connection, table: str, column: str
) -> None:
    row = conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    assert row is not None and row[0] == "numeric"


def test_untranslated_products_match_source(conn: psycopg.Connection) -> None:
    """Documented lookup gap: products whose category is absent from the
    translation file. The DB count must equal the count derived from CSVs."""
    products = pd.read_csv(DATA_DIR / "olist_products_dataset.csv",
                           usecols=["product_category_name"])
    translation = pd.read_csv(DATA_DIR / "product_category_name_translation.csv",
                              usecols=["product_category_name"])
    expected = int((
        ~products["product_category_name"].isin(translation["product_category_name"])
        & products["product_category_name"].notna()
    ).sum())
    db_n = conn.execute(
        "SELECT count(*) FROM products p WHERE p.product_category_name IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM product_category_translation t "
        "WHERE t.product_category_name = p.product_category_name)"
    ).fetchone()[0]
    assert db_n == expected


def test_orders_without_payment_match_source(conn: psycopg.Connection) -> None:
    orders = pd.read_csv(DATA_DIR / "olist_orders_dataset.csv", usecols=["order_id"])
    payments = pd.read_csv(DATA_DIR / "olist_order_payments_dataset.csv",
                           usecols=["order_id"])
    expected = int((~orders["order_id"].isin(payments["order_id"])).sum())
    db_n = conn.execute(
        "SELECT count(*) FROM orders o WHERE NOT EXISTS "
        "(SELECT 1 FROM order_payments p WHERE p.order_id = o.order_id)"
    ).fetchone()[0]
    assert db_n == expected


def test_order_status_values_match_source(conn: psycopg.Connection) -> None:
    csv_statuses = set(pd.read_csv(DATA_DIR / "olist_orders_dataset.csv",
                                   usecols=["order_status"])["order_status"])
    db_statuses = {r[0] for r in conn.execute(
        "SELECT DISTINCT order_status FROM orders"
    ).fetchall()}
    assert db_statuses == csv_statuses


def test_distinct_real_customers_match_source(conn: psycopg.Connection) -> None:
    expected = pd.read_csv(DATA_DIR / "olist_customers_dataset.csv",
                           usecols=["customer_unique_id"])["customer_unique_id"].nunique()
    db_n = conn.execute(
        "SELECT count(DISTINCT customer_unique_id) FROM customers"
    ).fetchone()[0]
    assert db_n == expected
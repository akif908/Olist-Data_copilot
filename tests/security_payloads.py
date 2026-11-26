"""Categorized adversarial SQL corpus for the Phase 5 validator (L2).

These are *static* payloads — no database connection is needed. Each entry is
(sql, expected_rejection_code). The codes let tests and reports distinguish WHY
a query was rejected (refinement #6), not merely that it was.

The real security boundary is the read-only PostgreSQL role (L3), tested live
and independently in Step 4. This corpus exercises the redundant early-rejection
layer: obfuscation via comments, casing, whitespace, quoting, nesting, CTEs and
schema qualification must not bypass structural AST inspection.
"""
from __future__ import annotations

from app.security import sql_validator as v

# Valid, approved read-only queries that MUST pass (no rewriting; original text
# is preserved). Covers CTEs, subqueries, UNION, aliases, ORDER BY/OFFSET and
# explicit public-schema qualification.
VALID_QUERIES: list[str] = [
    "SELECT count(*) AS order_count FROM orders",
    "SELECT order_status, count(*) AS n FROM orders GROUP BY order_status ORDER BY n DESC",
    "SELECT customer_state, count(*) AS n FROM customers GROUP BY customer_state",
    # CTE + join + aggregate
    "WITH rev AS (SELECT order_id, avg(review_score) AS s FROM order_reviews GROUP BY order_id) "
    "SELECT o.order_status, avg(r.s) AS avg_score FROM orders o JOIN rev r ON o.order_id = r.order_id "
    "GROUP BY o.order_status",
    # scalar subquery
    "SELECT order_id FROM orders WHERE order_id IN (SELECT order_id FROM order_items)",
    # UNION of two approved tables' columns
    "SELECT customer_id AS id FROM customers UNION SELECT order_id AS id FROM orders",
    # explicit public schema qualification
    "SELECT count(*) FROM public.orders",
    # ORDER BY + LIMIT + OFFSET (must be preserved, never rewritten)
    "SELECT product_id, price FROM order_items ORDER BY price DESC LIMIT 10 OFFSET 5",
    # allowed functions
    "SELECT date_trunc('month', order_purchase_timestamp) AS m, count(*) "
    "FROM orders GROUP BY 1 ORDER BY 1",
    "SELECT round(avg(price), 2) AS avg_price FROM order_items",
    "SELECT coalesce(product_category_name, 'unknown') AS c, count(*) "
    "FROM products GROUP BY 1",
    # quoted identifiers / mixed casing
    'SELECT "order_id" FROM "orders"',
    "SeLeCt COUNT(*) FrOm OrDeRs",
]

# Attacks grouped by category. Each value is a list of (sql, expected_code).
ATTACKS: dict[str, list[tuple[str, str]]] = {
    "dml": [
        ("UPDATE orders SET order_status = 'delivered'", v.NOT_SELECT),
        ("DELETE FROM orders", v.NOT_SELECT),
        ("INSERT INTO orders (order_id) VALUES ('x')", v.NOT_SELECT),
        ("MERGE INTO orders USING customers ON (1=1) WHEN MATCHED THEN DELETE", v.NOT_SELECT),
    ],
    "ddl": [
        ("DROP TABLE orders", v.NOT_SELECT),
        ("CREATE TABLE evil (id int)", v.NOT_SELECT),
        ("ALTER TABLE orders ADD COLUMN pwned text", v.NOT_SELECT),
        ("TRUNCATE TABLE orders", v.NOT_SELECT),
        ("CREATE INDEX ix ON orders (order_id)", v.NOT_SELECT),
    ],
    "multi_statement": [
        ("SELECT 1; SELECT 2", v.MULTIPLE_STATEMENTS),
        ("SELECT count(*) FROM orders; DROP TABLE orders", v.MULTIPLE_STATEMENTS),
        ("SELECT 1;; SELECT 2", v.MULTIPLE_STATEMENTS),
    ],
    "catalog": [
        ("SELECT * FROM pg_catalog.pg_tables", v.CATALOG_ACCESS),
        ("SELECT * FROM information_schema.tables", v.CATALOG_ACCESS),
        ("SELECT * FROM pg_catalog.pg_authid", v.CATALOG_ACCESS),
        ('SELECT * FROM "pg_catalog"."pg_tables"', v.CATALOG_ACCESS),
    ],
    "dangerous_function": [
        ("SELECT pg_sleep(10)", v.DANGEROUS_FUNCTION),
        ("SELECT pg_read_file('/etc/passwd')", v.DANGEROUS_FUNCTION),
        ("SELECT lo_import('/etc/passwd')", v.DANGEROUS_FUNCTION),
        ("SELECT dblink('dbname=postgres', 'SELECT 1')", v.DANGEROUS_FUNCTION),
        ("SELECT set_config('statement_timeout', '0', false)", v.DANGEROUS_FUNCTION),
        ("SELECT current_setting('is_superuser')", v.DANGEROUS_FUNCTION),
        ("SELECT pg_catalog.pg_sleep(1)", v.DANGEROUS_FUNCTION),
        ("SELECT pg_terminate_backend(1)", v.DANGEROUS_FUNCTION),
        ("SELECT unnest(array[1,2,3])", v.DANGEROUS_FUNCTION),
    ],
    "table_function": [
        ("SELECT * FROM generate_series(1, 10)", v.TABLE_FUNCTION),
        ("SELECT * FROM generate_series(1,10) AS g", v.TABLE_FUNCTION),
        ("SELECT * FROM orders, generate_series(1,3)", v.TABLE_FUNCTION),
    ],
    "select_into": [
        ("SELECT * INTO tmp FROM orders", v.FORBIDDEN_NODE),
        ("SELECT order_id INTO TEMPORARY TABLE t FROM orders", v.FORBIDDEN_NODE),
    ],
    "row_lock": [
        ("SELECT * FROM orders FOR UPDATE", v.FORBIDDEN_NODE),
        ("SELECT * FROM orders FOR SHARE", v.FORBIDDEN_NODE),
        ("SELECT * FROM orders FOR NO KEY UPDATE", v.FORBIDDEN_NODE),
    ],
    "data_modifying_cte": [
        ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", v.FORBIDDEN_NODE),
        ("WITH u AS (UPDATE orders SET order_status='x' RETURNING *) SELECT * FROM u", v.FORBIDDEN_NODE),
        ("WITH i AS (INSERT INTO orders (order_id) VALUES ('x') RETURNING *) SELECT * FROM i", v.FORBIDDEN_NODE),
    ],
    "unknown_schema_object": [
        ("SELECT * FROM sellers", v.UNKNOWN_TABLE),
        ("SELECT * FROM geolocation", v.UNKNOWN_TABLE),
        ("SELECT * FROM secret_table", v.UNKNOWN_TABLE),
        ("SELECT nonexistent_column FROM orders", v.UNKNOWN_COLUMN),
        ("SELECT order_id, pwned FROM orders", v.UNKNOWN_COLUMN),
    ],
    "obfuscated": [
        # comment / whitespace / casing obfuscation of a write
        ("/* hi */ UPDATE orders SET order_status='x'", v.NOT_SELECT),
        ("SELECT 1; -- trailing\n DROP TABLE orders", v.MULTIPLE_STATEMENTS),
        # nested data-modifying CTE hidden behind a valid outer SELECT
        ("WITH x AS (WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d) SELECT * FROM x",
         v.FORBIDDEN_NODE),
        # dangerous function nested in a subquery
        ("SELECT * FROM orders WHERE order_id = (SELECT pg_sleep(1))", v.DANGEROUS_FUNCTION),
        # catalog access nested in a subquery
        ("SELECT * FROM orders WHERE order_id IN (SELECT tablename FROM pg_catalog.pg_tables)",
         v.CATALOG_ACCESS),
    ],
    "empty": [
        ("", v.EMPTY),
        ("   ", v.EMPTY),
        ("-- just a comment", v.EMPTY),
    ],
}
# Phase 5 Step 4 — Read-only role permission isolation (live)

Tests run **only** through `get_readonly_connection()` as `copilot_ro`, bypassing
SQLGlot and the application validator entirely. The database itself is the
security boundary under test. File: `tests/test_db_permissions_live.py`
(28 tests, `live` marker). Command:

```
python -m pytest tests/test_db_permissions_live.py -v -s -m live
28 passed in 2.91s
```

## Connection identity

```
SELECT current_user, session_user  ->  ('copilot_ro', 'copilot_ro')
rolsuper for current_user          ->  False
```

No admin connection is used anywhere in these tests.

## Error-class legend (SQLSTATE)

| SQLSTATE | Meaning |
|---|---|
| 25006 | read_only_sql_transaction — rejected because the transaction is read-only |
| 42501 | insufficient_privilege — rejected by the role's GRANTs (permission denied) |
| 57014 | query_canceled — statement_timeout fired |
| 42P01 | undefined_table — relation does not exist |

## 1. SELECT on the seven authorized tables — succeeds

`SELECT count(*) FROM <table>` returned OK for all seven:
customers 99441, order_items 112650, order_payments 103886, order_reviews 99224,
orders 99441, product_category_translation 71, products 32951.

## 2. INSERT / UPDATE / DELETE — fail (both guards demonstrated)

Read-only transaction (default):

```
INSERT INTO orders (...) VALUES ('__probe__', ...)   -> 25006  cannot execute INSERT in a read-only transaction
UPDATE orders SET order_status='delivered' WHERE order_id='__nonexistent_probe__' -> 25006  cannot execute UPDATE in a read-only transaction
DELETE FROM orders WHERE order_id='__nonexistent_probe__' -> 25006  cannot execute DELETE in a read-only transaction
```

Explicit `BEGIN READ WRITE` (overrides the read-only GUC, isolating the GRANT boundary):

```
INSERT ... -> 42501  permission denied for table orders
UPDATE ... -> 42501  permission denied for table orders
DELETE ... -> 42501  permission denied for table orders
```

## 3. DDL — fail by permission (read-write session)

```
CREATE TABLE _probe_should_not_exist (id int) -> 42501  permission denied for schema public
DROP TABLE orders                           -> 42501  must be owner of table orders
ALTER TABLE orders ADD COLUMN _probe_col int-> 42501  must be owner of table orders
TRUNCATE TABLE orders                       -> 42501  permission denied for table orders
```

## 4. CREATE TEMP TABLE — fail (PUBLIC TEMPORARY revoked)

```
read-write:  CREATE TEMP TABLE _probe_tmp (id int) -> 42501  permission denied to create temporary tables in database "olist_copilot"
read-only:   CREATE TEMP TABLE _probe_tmp (id int) -> 25006  cannot execute CREATE TABLE in a read-only transaction
```

The 42501 in read-write mode proves the revocation is a hard permission boundary,
not merely a read-only-transaction effect.

## 5. Catalog access — inspected separately (honest nuance)

There is **no eighth user table** in `public` (confirmed: exactly the seven), and
none was created. System catalogs, however, **are readable by the role** because
PostgreSQL grants PUBLIC catalog reads by default:

```
SELECT count(*) FROM pg_catalog.pg_tables      -> OK (75)
SELECT count(*) FROM pg_catalog.pg_roles       -> OK (18)
SELECT count(*) FROM information_schema.tables -> OK (198)
SELECT * FROM _no_such_user_table_             -> 42P01  relation does not exist
```

**Conclusion:** the role is NOT limited to exactly seven readable *relations* at
the database level — system catalogs remain readable. Restricting catalog access
is enforced by the L2 SQLGlot validator (`catalog_access` rejection), not by L3
grants. This is stated explicitly rather than over-claiming isolation.

## 6. Statement timeout — verified independently

```
SHOW statement_timeout                     -> 10s
SHOW lock_timeout                          -> 5s
SHOW idle_in_transaction_session_timeout   -> 30s
SHOW default_transaction_read_only         -> on
SET statement_timeout='1s'; SELECT pg_sleep(3) -> 57014  canceling statement due to statement timeout
```

## 7. Data unchanged

Row counts captured via `copilot_ro` before and after a battery of failing
write/DDL/temp attempts (all rolled back) are identical for all seven tables.
No table was created and no privilege was modified during testing.

## Regression

`python -m pytest -q -m "not live"` → `205 passed, 38 deselected`. Phases 1–4
functionality preserved.
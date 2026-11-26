"""Phase 5 Step 4 — LIVE database-permission isolation tests (security layer L3).

These tests connect ONLY through get_readonly_connection() as the dedicated
read-only role (copilot_ro). They deliberately BYPASS SQLGlot and the application
validator: the goal is to prove the database itself enforces isolation, independent
of any application-layer check.

They distinguish PostgreSQL error classes by SQLSTATE (refinement #6):
  * 25006 read_only_sql_transaction  — rejected because the transaction is read-only
  * 42501 insufficient_privilege     — rejected by the role's GRANTs (permission denied)
  * 57014 query_canceled             — statement_timeout fired
  * 42P01 undefined_table            — relation does not exist

Safety: every write/DDL attempt is rolled back; WHERE clauses match no rows; no
table is created and no privilege is modified to manufacture a test. System-catalog
readability is inspected separately and reported honestly (the role is NOT claimed
to be limited to exactly seven readable relations unless demonstrated).

Skipped automatically when PG_RO_PASSWORD is not configured.
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from app.config import ReadOnlyDatabaseSettings
from app.database.connection import get_readonly_connection
from app.security.allowlist import SchemaAllowlist

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not ReadOnlyDatabaseSettings.from_env().password,
        reason="PG_RO_PASSWORD not configured in .env",
    ),
]

TABLES = sorted(SchemaAllowlist.load().tables)

READ_ONLY_SQL_TRANSACTION = "25006"
INSUFFICIENT_PRIVILEGE = "42501"
QUERY_CANCELED = "57014"


def _attempt(conn, sql: str, params=None):
    """Execute sql, capture (status, sqlstate, first-line message), then roll back.

    Prints the exact SQL and outcome so the test output is an auditable report.
    Nothing is committed; the transaction is always rolled back.
    """
    label = sql if params is None else f"{sql}  params={params!r}"
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
        conn.rollback()
        print(f"\n[OK]    {label}\n        -> rows={rows}")
        return ("OK", None, None, rows)
    except psycopg.Error as exc:
        conn.rollback()
        msg = str(exc).strip().splitlines()[0]
        print(f"\n[ERROR] {label}\n        -> SQLSTATE={exc.sqlstate}  {msg}")
        return ("ERROR", exc.sqlstate, msg, None)


@contextmanager
def _read_write_session():
    """A copilot_ro session with an explicit READ WRITE transaction.

    BEGIN READ WRITE overrides the role's default_transaction_read_only=on GUC so
    we can observe the *permission* boundary (42501) rather than a read-only
    transaction rejection (25006). This is a session/transaction control, NOT a
    privilege change. autocommit lets us control each transaction explicitly.
    """
    with get_readonly_connection(read_only=False) as conn:
        conn.autocommit = True
        yield conn


def _attempt_rw(conn, sql: str, params=None):
    conn.execute("BEGIN READ WRITE")
    return _attempt(conn, sql, params)


# --- 0. connection identity -------------------------------------------------

def test_connection_identity_is_readonly_role():
    with get_readonly_connection() as conn:
        status, _, _, rows = _attempt(conn, "SELECT current_user, session_user")
        assert status == "OK"
        assert rows[0][0] == "copilot_ro" and rows[0][1] == "copilot_ro"
        sup = conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]
        conn.rollback()
        print(f"\n        current_user is superuser: {sup}")
        assert sup is False


# --- 1. SELECT on each of the seven authorized tables succeeds --------------

@pytest.mark.parametrize("table", TABLES)
def test_select_authorized_table_succeeds(table):
    with get_readonly_connection() as conn:
        status, _, _, rows = _attempt(conn, f"SELECT count(*) FROM {table}")
        assert status == "OK"
        assert isinstance(rows[0][0], int)


# --- 2. INSERT / UPDATE / DELETE fail ---------------------------------------

WRITE_STATEMENTS = [
    ("INSERT", "INSERT INTO orders (order_id, customer_id, order_status, "
               "order_purchase_timestamp, order_estimated_delivery_date) "
               "VALUES ('__probe__', '__probe__', 'created', now(), now())"),
    ("UPDATE", "UPDATE orders SET order_status = 'delivered' "
               "WHERE order_id = '__nonexistent_probe__'"),
    ("DELETE", "DELETE FROM orders WHERE order_id = '__nonexistent_probe__'"),
]


@pytest.mark.parametrize("name,sql", WRITE_STATEMENTS, ids=[w[0] for w in WRITE_STATEMENTS])
def test_write_rejected_in_readonly_transaction(name, sql):
    # Default read-only transaction => 25006 (read-only), proving the txn guard.
    with get_readonly_connection() as conn:
        status, code, _, _ = _attempt(conn, sql)
        assert status == "ERROR"
        assert code == READ_ONLY_SQL_TRANSACTION


@pytest.mark.parametrize("name,sql", WRITE_STATEMENTS, ids=[w[0] for w in WRITE_STATEMENTS])
def test_write_rejected_by_permission_in_readwrite(name, sql):
    # Explicit READ WRITE txn => 42501 (permission denied), proving the GRANT
    # boundary independently of the read-only transaction guard.
    with _read_write_session() as conn:
        status, code, _, _ = _attempt_rw(conn, sql)
        assert status == "ERROR"
        assert code == INSUFFICIENT_PRIVILEGE


# --- 3. DDL fails -----------------------------------------------------------

DDL_STATEMENTS = [
    ("CREATE TABLE", "CREATE TABLE _probe_should_not_exist (id int)"),
    ("DROP TABLE", "DROP TABLE orders"),
    ("ALTER TABLE", "ALTER TABLE orders ADD COLUMN _probe_col int"),
    ("TRUNCATE", "TRUNCATE TABLE orders"),
]


@pytest.mark.parametrize("name,sql", DDL_STATEMENTS, ids=[d[0] for d in DDL_STATEMENTS])
def test_ddl_rejected_by_permission(name, sql):
    with _read_write_session() as conn:
        status, code, _, _ = _attempt_rw(conn, sql)
        assert status == "ERROR"
        assert code == INSUFFICIENT_PRIVILEGE


# --- 4. CREATE TEMP TABLE fails (PUBLIC TEMPORARY revoked) ------------------

def test_create_temp_rejected_by_permission_in_readwrite():
    with _read_write_session() as conn:
        status, code, msg, _ = _attempt_rw(conn, "CREATE TEMP TABLE _probe_tmp (id int)")
        assert status == "ERROR"
        assert code == INSUFFICIENT_PRIVILEGE
        assert "temporary" in msg.lower()


def test_create_temp_rejected_in_readonly_transaction():
    with get_readonly_connection() as conn:
        status, code, _, _ = _attempt(conn, "CREATE TEMP TABLE _probe_tmp (id int)")
        assert status == "ERROR"
        assert code == READ_ONLY_SQL_TRANSACTION


# --- 5. catalog access inspected separately (honest nuance) -----------------

def test_no_eighth_user_table_exists():
    # There is no extra *user* table outside the allowlist to test against; we
    # must not create one. Confirm public contains exactly the seven tables.
    with get_readonly_connection() as conn:
        status, _, _, rows = _attempt(
            conn,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename",
        )
        assert status == "OK"
        names = [r[0] for r in rows]
        assert names == TABLES


@pytest.mark.parametrize("catalog", [
    "pg_catalog.pg_tables",
    "pg_catalog.pg_roles",
    "information_schema.tables",
])
def test_system_catalog_reads_are_permitted_by_default(catalog):
    # PostgreSQL grants PUBLIC read access to many catalogs. The ROLE can read
    # them; it is the L2 validator that blocks catalog access. We demonstrate and
    # report this honestly rather than claiming the role sees only seven relations.
    with get_readonly_connection() as conn:
        status, _, _, rows = _attempt(conn, f"SELECT count(*) FROM {catalog}")
        assert status == "OK"
        assert isinstance(rows[0][0], int)


def test_ungranted_user_relation_does_not_exist():
    # A non-existent user table yields undefined_table (42P01), not a permission
    # error — there is no eighth granted/ungranted user table in this database.
    with get_readonly_connection() as conn:
        status, code, _, _ = _attempt(conn, "SELECT * FROM _no_such_user_table_")
        assert status == "ERROR"
        assert code == "42P01"


# --- 6. statement timeout verified independently ----------------------------

def test_role_timeout_settings_present():
    with get_readonly_connection() as conn:
        for guc, expected in [
            ("statement_timeout", "10s"),
            ("lock_timeout", "5s"),
            ("idle_in_transaction_session_timeout", "30s"),
            ("default_transaction_read_only", "on"),
        ]:
            status, _, _, rows = _attempt(conn, f"SHOW {guc}")
            assert status == "OK"
            assert rows[0][0] == expected, f"{guc}: {rows[0][0]} != {expected}"


def test_statement_timeout_actually_fires():
    # Independently demonstrate enforcement: shrink the session timeout to 1s and
    # run a 3s sleep; PostgreSQL must cancel it with 57014. pg_sleep is permitted
    # for the role (catalog/function EXECUTE is public by default); this probes the
    # DB-level timeout, not the validator. Nothing persists.
    with get_readonly_connection() as conn:
        conn.execute("SET statement_timeout = '1s'")
        status, code, _, _ = _attempt(conn, "SELECT pg_sleep(3)")
        assert status == "ERROR"
        assert code == QUERY_CANCELED


# --- 7. database state unchanged after all write/DDL attempts ---------------

def test_data_unchanged_after_write_attempts():
    with get_readonly_connection() as conn:
        before = {}
        for t in TABLES:
            before[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        conn.rollback()

    # Run a battery of failing write/DDL/temp attempts in read-write mode.
    with _read_write_session() as conn:
        for _name, sql in WRITE_STATEMENTS + DDL_STATEMENTS:
            _attempt_rw(conn, sql)
        _attempt_rw(conn, "CREATE TEMP TABLE _probe_tmp (id int)")

    with get_readonly_connection() as conn:
        after = {}
        for t in TABLES:
            after[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        conn.rollback()

    print(f"\n        before={before}\n        after ={after}")
    assert before == after
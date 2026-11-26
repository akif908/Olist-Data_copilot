"""Phase 5 Step 5 — offline unit tests for the safe executor and audit logging.

No database is required: the read-only connection is replaced with a fake so we
can test clamping, truncation logic, rollback/cleanup and admin-isolation
deterministically. Audit sanitization is tested with deliberately secret-like
values. Live behavior is covered by tests/test_executor_live.py.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest

from app.database import executor
from app.database.executor import (
    MAX_ROWS,
    ExecutionError,
    execute_sql,
)
from app.security import audit


# --- fake read-only connection ---------------------------------------------

class _FakeCursor:
    def __init__(self, rows, columns):
        self._rows = rows
        self.description = [SimpleNamespace(name=c) for c in columns]

    def fetchmany(self, n):
        return self._rows[:n]


class _FakeConn:
    def __init__(self, rows=None, columns=("id",), query_exc=None):
        self.rows = rows if rows is not None else [(1,)]
        self.columns = columns
        self.query_exc = query_exc
        self.executed = []
        self.rolled_back = False
        self.closed = False
        self.read_only = None

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if sql.strip().upper().startswith("SET"):
            return None
        if self.query_exc is not None:
            raise self.query_exc
        return _FakeCursor(self.rows, self.columns)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _patch_conn(monkeypatch, conn):
    @contextmanager
    def fake_ro(settings=None, *, read_only=True):
        conn.read_only = read_only
        try:
            yield conn
        finally:
            conn.close()
    monkeypatch.setattr(executor, "get_readonly_connection", fake_ro)
    return conn


# --- clamping and truncation logic -----------------------------------------

def test_max_rows_clamped_to_hard_cap(monkeypatch):
    conn = _patch_conn(monkeypatch, _FakeConn(rows=[(i,) for i in range(MAX_ROWS + 1)]))
    r = execute_sql("SELECT id FROM t", validate=False, max_rows=5000)
    assert r.max_rows == MAX_ROWS
    assert r.row_count == MAX_ROWS
    assert r.truncated is True
    assert conn.read_only is True


@pytest.mark.parametrize("available,expect_count,expect_trunc", [
    (4, 4, False),   # max_rows - 1
    (5, 5, False),   # max_rows
    (6, 5, True),    # max_rows + 1
])
def test_truncation_boundaries(monkeypatch, available, expect_count, expect_trunc):
    _patch_conn(monkeypatch, _FakeConn(rows=[(i,) for i in range(available)]))
    r = execute_sql("SELECT id FROM t", validate=False, max_rows=5)
    assert r.row_count == expect_count
    assert r.truncated is expect_trunc
    assert len(r.rows) == expect_count


def test_invalid_max_rows_rejected():
    with pytest.raises(ValueError):
        execute_sql("SELECT 1", validate=False, max_rows=0)
    with pytest.raises(ValueError):
        execute_sql("SELECT 1", validate=False, max_rows=-3)


def test_statement_timeout_cannot_be_weakened(monkeypatch):
    conn = _patch_conn(monkeypatch, _FakeConn())
    execute_sql("SELECT id FROM t", validate=False, statement_timeout_ms=60000)
    assert "SET LOCAL statement_timeout = '10000ms'" in conn.executed
    assert "SET LOCAL lock_timeout = '5000ms'" in conn.executed
    assert any("idle_in_transaction_session_timeout" in s for s in conn.executed)


def test_shorter_timeout_allowed(monkeypatch):
    conn = _patch_conn(monkeypatch, _FakeConn())
    execute_sql("SELECT id FROM t", validate=False, statement_timeout_ms=1000)
    assert "SET LOCAL statement_timeout = '1000ms'" in conn.executed


# --- error handling, rollback, cleanup -------------------------------------

def test_error_rolls_back_and_closes(monkeypatch):
    conn = _patch_conn(monkeypatch, _FakeConn(
        query_exc=psycopg.errors.InsufficientPrivilege("permission denied for table orders")
    ))
    with pytest.raises(ExecutionError) as ei:
        execute_sql("SELECT id FROM orders", validate=False)
    assert ei.value.code == "permission_denied"
    assert ei.value.sqlstate == "42501"
    assert conn.rolled_back is True
    assert conn.closed is True


@pytest.mark.parametrize("exc,expected_code", [
    (psycopg.errors.QueryCanceled("canceling statement due to statement timeout"), "statement_timeout"),
    (psycopg.errors.ReadOnlySqlTransaction("cannot execute INSERT in a read-only transaction"), "read_only_transaction"),
    (psycopg.errors.UndefinedTable("relation does not exist"), "undefined_table"),
    (psycopg.errors.InsufficientPrivilege("permission denied"), "permission_denied"),
])
def test_error_code_mapping(monkeypatch, exc, expected_code):
    _patch_conn(monkeypatch, _FakeConn(query_exc=exc))
    with pytest.raises(ExecutionError) as ei:
        execute_sql("SELECT id FROM t", validate=False)
    assert ei.value.code == expected_code


def test_no_silent_retry_on_failure(monkeypatch):
    conn = _patch_conn(monkeypatch, _FakeConn(query_exc=psycopg.errors.QueryCanceled("timeout")))
    with pytest.raises(ExecutionError):
        execute_sql("SELECT id FROM t", validate=False)
    # exactly one attempt at the query (plus the SET LOCAL guardrails), no retry
    query_attempts = [s for s in conn.executed if not s.strip().upper().startswith("SET")]
    assert len(query_attempts) == 1


# --- admin connection isolation --------------------------------------------

def test_executor_never_uses_admin_connection(monkeypatch):
    import app.database.connection as connection

    def _boom(*a, **k):
        raise AssertionError("admin get_connection must never be used by the executor")

    monkeypatch.setattr(connection, "get_connection", _boom)
    _patch_conn(monkeypatch, _FakeConn())
    r = execute_sql("SELECT id FROM t", validate=False)
    assert r.row_count == 1
    assert not hasattr(executor, "get_connection")


# --- audit sanitization -----------------------------------------------------

def test_sanitize_known_env_secret(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "supersecretvalue123")
    out = audit.sanitize_text("connection failed password=supersecretvalue123")
    assert "supersecretvalue123" not in out
    assert audit.REDACTED in out


@pytest.mark.parametrize("text", [
    "api_key=sk-or-v1-abcdef1234567890abcdef",
    "Authorization: Bearer abc.def.ghi",
    "postgresql://user:pass@host:5432/db",
    "PG_RO_PASSWORD=topsecret123",
    "token: abcdef123456",
])
def test_sanitize_secret_patterns(text):
    out = audit.sanitize_text(text)
    assert "pass@host" not in out
    assert "abc.def.ghi" not in out
    assert "sk-or-v1-abcdef1234567890abcdef" not in out
    assert "topsecret123" not in out
    assert "abcdef123456" not in out


def test_redact_sql_literals():
    sql = "SELECT * FROM customers WHERE customer_id = 'abc123' AND customer_zip_code_prefix = 12345"
    out = audit.redact_sql_literals(sql)
    assert "abc123" not in out
    assert "12345" not in out
    assert "customers" in out and "customer_id" in out
    assert "?" in out


def test_audit_execution_redacts_literals_and_secrets(monkeypatch, caplog):
    monkeypatch.setenv("PGPASSWORD", "supersecretvalue123")
    caplog.set_level(logging.INFO, logger="copilot.audit")
    rec = audit.audit_execution(
        sql="SELECT * FROM customers WHERE customer_id = 'secret123'",
        status="error", row_count=0, truncated=False, max_rows=1000,
        error_code="permission_denied",
        error_message="permission denied; password=supersecretvalue123",
    )
    assert "secret123" not in rec.sql_redacted
    assert "supersecretvalue123" not in (rec.error_message or "")
    assert "supersecretvalue123" not in caplog.text
    assert "secret123" not in caplog.text
    # no raw results are ever part of the record
    assert not hasattr(rec, "rows")


def test_audit_record_fields():
    rec = audit.audit_execution(
        sql="SELECT count(*) FROM orders", status="ok", tables=("orders",),
        duration_ms=12.3, row_count=1, truncated=False, max_rows=1000,
    )
    assert rec.event == "execution" and rec.status == "ok"
    assert rec.row_count == 1 and rec.truncated is False and rec.max_rows == 1000
    assert rec.tables == ("orders",)
    assert rec.duration_ms == 12.3
    assert len(rec.sql_fingerprint) == 16
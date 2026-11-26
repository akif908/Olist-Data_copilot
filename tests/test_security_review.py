"""Phase 5 Step 5 — focused security review tests (four areas).

1. Audit sink: propagation/handler behavior; events reach a real configured sink;
   no silent claim of persistence while only a NullHandler exists.
2. SQL redaction/fingerprinting: stable fingerprint across literal variations;
   comments containing secrets removed; malformed/parse-failure SQL never echoed.
3. Exception sanitization: real PostgreSQL and connection-error paths; passwords,
   API keys, connection strings, quoted values and unusual formats redacted.
4. Transaction/timeout isolation: caller cannot disable read-only or exceed the
   timeout ceiling; fresh connection per request; no open transaction left behind.

Offline tests run always; live tests are marked and skip without PG_RO_PASSWORD.
"""
from __future__ import annotations

import inspect
import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest

from app.config import ReadOnlyDatabaseSettings
from app.database import executor
from app.database.executor import ExecutionError, execute_sql
from app.security import audit

RO_CONFIGURED = bool(ReadOnlyDatabaseSettings.from_env().password)
live = pytest.mark.live
needs_ro = pytest.mark.skipif(not RO_CONFIGURED, reason="PG_RO_PASSWORD not configured")


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _FakeCursor:
    def __init__(self, rows, columns):
        self._rows = rows
        self.description = [SimpleNamespace(name=c) for c in columns]

    def fetchmany(self, n):
        return self._rows[:n]


class _FakeConn:
    def __init__(self, rows=None, query_exc=None):
        self.rows = rows if rows is not None else [(1,)]
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
        return _FakeCursor(self.rows, ("id",))

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


# === 1. AUDIT SINK ==========================================================

def test_default_logger_nullhandler_and_propagation():
    # Honest default: a NullHandler exists and propagation is on, but there is no
    # real sink until the application configures one.
    assert any(isinstance(h, logging.NullHandler) for h in audit.AUDIT_LOGGER.handlers)
    assert audit.AUDIT_LOGGER.propagate is True


def test_events_reach_configured_sink():
    h = _ListHandler()
    audit.configure_audit_logging(h)
    try:
        audit.audit_execution(sql="SELECT count(*) FROM orders", status="ok", row_count=1)
        assert h.records, "configured sink received no event"
        payload = json.loads(h.records[-1].getMessage())
        assert payload["event"] == "execution" and payload["status"] == "ok"
    finally:
        audit.AUDIT_LOGGER.removeHandler(h)


def test_configure_audit_logging_idempotent():
    h = _ListHandler()
    audit.configure_audit_logging(h)
    n = len(audit.AUDIT_LOGGER.handlers)
    audit.configure_audit_logging(h)
    try:
        assert len(audit.AUDIT_LOGGER.handlers) == n  # not duplicated
    finally:
        audit.AUDIT_LOGGER.removeHandler(h)


# === 2. SQL REDACTION / FINGERPRINT =========================================

def test_fingerprint_stable_across_literal_variations():
    r1 = audit.audit_execution(sql="SELECT * FROM customers WHERE customer_id='aaa'", status="ok")
    r2 = audit.audit_execution(sql="SELECT * FROM customers WHERE customer_id='bbb'", status="ok")
    assert r1.sql_fingerprint == r2.sql_fingerprint
    assert r1.sql_redacted == r2.sql_redacted
    assert "aaa" not in r1.sql_redacted and "bbb" not in r2.sql_redacted


def test_comments_containing_secrets_are_removed(caplog):
    caplog.set_level(logging.INFO, logger="copilot.audit")
    sql = "SELECT count(*) FROM orders /* barevalue123 password=topsecret123 */"
    rec = audit.audit_execution(sql=sql, status="ok")
    assert "barevalue123" not in rec.sql_redacted
    assert "topsecret123" not in rec.sql_redacted
    assert "/*" not in rec.sql_redacted          # comment stripped entirely
    assert "barevalue123" not in caplog.text


def test_malformed_sql_never_echoed(caplog):
    caplog.set_level(logging.INFO, logger="copilot.audit")
    sql = "SELECT FROM WHERE password='leakme123'"
    assert audit.redact_sql_literals(sql) == "<unparseable>"
    rec = audit.audit_execution(sql=sql, status="error", error_message="parse failed")
    assert rec.sql_redacted == "<unparseable>"
    assert "leakme123" not in caplog.text
    assert "SELECT FROM WHERE" not in caplog.text


def test_empty_sql_placeholder():
    assert audit.redact_sql_literals("") in {"<empty>", "<unparseable>"}


def test_redaction_keeps_structure_drops_literals():
    out = audit.redact_sql_literals(
        "SELECT customer_id FROM customers WHERE customer_zip_code_prefix = 12345"
    )
    assert "12345" not in out
    assert "customers" in out and "customer_id" in out and "?" in out


# === 3. EXCEPTION SANITIZATION =============================================

def test_sanitize_error_redacts_quoted_value():
    out = audit.sanitize_error('invalid input syntax for type integer: "leakme123"')
    assert "leakme123" not in out
    assert "invalid input syntax for type integer" in out  # useful detail kept


@pytest.mark.parametrize("text,secret", [
    ("password=supersecretvalue123", "supersecretvalue123"),
    ("api_key=sk-or-v1-abcdef1234567890abcdef", "sk-or-v1-abcdef1234567890abcdef"),
    ("postgresql://user:pass@host:5432/db", "pass@host"),
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ('FATAL: password authentication failed for user "copilot_ro"', "copilot_ro"),
    ('connection to server at "127.0.0.1", port 5432', "127.0.0.1"),
])
def test_sanitize_error_formats(text, secret):
    assert secret not in audit.sanitize_error(text)


def test_known_env_secret_redacted_even_unusual_format(monkeypatch):
    monkeypatch.setenv("PG_RO_PASSWORD", "x7#unusualFormat!value")
    out = audit.sanitize_error("detail mentions x7#unusualFormat!value somewhere")
    assert "x7#unusualFormat!value" not in out


@live
@needs_ro
def test_live_pg_error_literal_not_leaked(caplog):
    caplog.set_level(logging.INFO, logger="copilot.audit")
    with pytest.raises(ExecutionError) as ei:
        execute_sql("SELECT 'leakme123'::int", validate=False)
    assert "leakme123" not in ei.value.message
    assert "leakme123" not in caplog.text


@live
@needs_ro
def test_live_connection_error_exposes_no_password():
    bad = ReadOnlyDatabaseSettings(
        host="localhost", port=5432, dbname="olist_copilot",
        user="copilot_ro", password="wrongpass_should_not_appear",
    )
    with pytest.raises(ExecutionError) as ei:
        execute_sql("SELECT 1", validate=False, settings=bad)
    assert ei.value.code == "connection_error"
    assert "wrongpass_should_not_appear" not in ei.value.message


# === 4. TRANSACTION / TIMEOUT ISOLATION ====================================

def test_executor_exposes_no_readonly_override():
    params = inspect.signature(execute_sql).parameters
    assert "read_only" not in params  # caller cannot disable read-only execution


def test_readonly_forced_on_connection(monkeypatch):
    conn = _patch_conn(monkeypatch, _FakeConn())
    execute_sql("SELECT id FROM t", validate=False)
    assert conn.read_only is True


@pytest.mark.parametrize("requested", [10000, 60000, 999999])
def test_timeout_cannot_exceed_ceiling(monkeypatch, requested):
    conn = _patch_conn(monkeypatch, _FakeConn())
    execute_sql("SELECT id FROM t", validate=False, statement_timeout_ms=requested)
    assert "SET LOCAL statement_timeout = '10000ms'" in conn.executed
    assert not any(f"'{requested}ms'" in s for s in conn.executed if requested > 10000)


def test_success_path_rolls_back_and_closes(monkeypatch):
    conn = _patch_conn(monkeypatch, _FakeConn())
    execute_sql("SELECT id FROM t", validate=False)
    assert conn.rolled_back is True
    assert conn.closed is True


def test_failure_path_rolls_back_and_closes(monkeypatch):
    conn = _patch_conn(monkeypatch, _FakeConn(query_exc=psycopg.errors.QueryCanceled("timeout")))
    with pytest.raises(ExecutionError):
        execute_sql("SELECT id FROM t", validate=False)
    assert conn.rolled_back is True
    assert conn.closed is True


@live
@needs_ro
def test_live_connection_not_reused_between_requests():
    p1 = execute_sql("SELECT pg_backend_pid()", validate=False).rows[0][0]
    p2 = execute_sql("SELECT pg_backend_pid()", validate=False).rows[0][0]
    assert p1 != p2  # distinct backends => fresh connection per request


@live
@needs_ro
def test_live_readonly_enforced_even_when_validation_bypassed():
    with pytest.raises(ExecutionError) as ei:
        execute_sql("DELETE FROM orders WHERE order_id='__none__'", validate=False)
    assert ei.value.code in {"read_only_transaction", "permission_denied"}
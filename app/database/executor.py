"""Safe read-only SQL executor (Phase 5, Step 5 — security layer L4).

Executes already-generated SQL through the DEDICATED read-only role only
(``get_readonly_connection``); the admin connection is never used here. Guardrails:

* read-only transaction (role grants + role GUCs + session read_only);
* transaction-local timeouts via ``SET LOCAL`` (statement/lock/idle), clamped so a
  caller cannot weaken them beyond the configured maximum;
* hard cap of ``MAX_ROWS`` returned rows; ``fetchmany(max_rows + 1)`` detects
  truncation accurately and at most ``max_rows`` rows are returned;
* caller-supplied ``max_rows`` is clamped to the configured maximum;
* failures are rolled back, the connection is always closed, and there is NO
  silent retry with weaker restrictions;
* every outcome is recorded through the sanitized audit log (no secrets/results).

Validation (L2) is applied by default before execution; pass ``validate=False``
only to exercise the execution guardrails in isolation (e.g. timeout tests).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import psycopg

from app.config import ReadOnlyDatabaseSettings
from app.database.connection import get_readonly_connection
from app.security import audit
from app.security.allowlist import SchemaAllowlist
from app.security.sql_validator import SQLValidationError, validate_sql

MAX_ROWS = 1000
STATEMENT_TIMEOUT_MS = 10_000
LOCK_TIMEOUT_MS = 5_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 30_000

# SQLSTATE -> distinguishable execution error code (refinement #6).
_SQLSTATE_CODES = {
    "25006": "read_only_transaction",
    "42501": "permission_denied",
    "57014": "statement_timeout",
    "42P01": "undefined_table",
    "42703": "undefined_column",
    "42601": "syntax_error",
    "08000": "connection_exception",
    "08003": "connection_does_not_exist",
    "08006": "connection_failure",
}


class ExecutionError(Exception):
    """A database error occurred during safe execution (sanitized, no secrets)."""

    def __init__(self, message: str, code: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.sqlstate = sqlstate


@dataclass(frozen=True)
class ExecutionResult:
    sql: str                      # the original SQL executed (never rewritten)
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]       # at most max_rows rows
    row_count: int
    truncated: bool               # True if more rows existed than max_rows
    max_rows: int                 # effective cap actually applied
    duration_ms: float
    tables: tuple[str, ...] = ()
    status: str = "ok"


def _error_code(exc: psycopg.Error) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate in _SQLSTATE_CODES:
        return _SQLSTATE_CODES[sqlstate]
    if isinstance(exc, psycopg.OperationalError):
        return "connection_error"
    return "database_error"


def execute_sql(
    sql: str,
    *,
    max_rows: int = MAX_ROWS,
    statement_timeout_ms: int = STATEMENT_TIMEOUT_MS,
    validate: bool = True,
    allowlist: SchemaAllowlist | None = None,
    settings: ReadOnlyDatabaseSettings | None = None,
    tables: tuple[str, ...] = (),
) -> ExecutionResult:
    """Safely execute a read-only SELECT and return capped, audited results.

    Raises SQLValidationError if validation fails (when ``validate=True``), or
    ExecutionError on a database error. Never retries with weaker restrictions.
    """
    if not isinstance(max_rows, int) or max_rows < 1:
        raise ValueError("max_rows must be a positive integer")
    effective_max = min(max_rows, MAX_ROWS)            # caller cannot exceed the cap
    if not isinstance(statement_timeout_ms, int) or statement_timeout_ms < 1:
        raise ValueError("statement_timeout_ms must be a positive integer")
    effective_timeout = min(statement_timeout_ms, STATEMENT_TIMEOUT_MS)  # no weakening

    resolved_tables = tuple(tables)
    if validate:
        try:
            result = validate_sql(sql, allowlist)
        except SQLValidationError as exc:
            audit.audit_validation(
                sql=sql, status="rejected", error_code=exc.code, error_message=exc.reason
            )
            raise
        resolved_tables = result.tables

    started = time.perf_counter()
    try:
        with get_readonly_connection(settings, read_only=True) as conn:
            try:
                # Transaction-local guardrails (in addition to role-level GUCs).
                conn.execute(f"SET LOCAL statement_timeout = '{effective_timeout}ms'")
                conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
                conn.execute(
                    "SET LOCAL idle_in_transaction_session_timeout = "
                    f"'{IDLE_IN_TRANSACTION_TIMEOUT_MS}ms'"
                )
                cur = conn.execute(sql)
                columns = tuple(d.name for d in cur.description) if cur.description else ()
                fetched = cur.fetchmany(effective_max + 1)   # +1 to detect truncation
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                truncated = len(fetched) > effective_max
                rows = tuple(fetched[:effective_max])
                conn.rollback()   # read-only: release the transaction, persist nothing
            except psycopg.Error as exc:
                conn.rollback()
                code = _error_code(exc)
                sqlstate = getattr(exc, "sqlstate", None)
                message = audit.sanitize_error(str(exc).strip().splitlines()[0]) or "database error"
                audit.audit_execution(
                    sql=sql, status="error", tables=resolved_tables,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    max_rows=effective_max, error_code=code, error_message=message,
                )
                raise ExecutionError(message, code, sqlstate) from exc
    except ExecutionError:
        raise
    except psycopg.Error as exc:   # connection-level failure (outside the txn block)
        code = _error_code(exc)
        message = audit.sanitize_error(str(exc).strip().splitlines()[0]) or "connection error"
        audit.audit_execution(
            sql=sql, status="error", tables=resolved_tables,
            max_rows=effective_max, error_code=code, error_message=message,
        )
        raise ExecutionError(message, code, getattr(exc, "sqlstate", None)) from exc

    audit.audit_execution(
        sql=sql, status="ok", tables=resolved_tables, duration_ms=duration_ms,
        row_count=len(rows), truncated=truncated, max_rows=effective_max,
    )
    return ExecutionResult(
        sql=sql, columns=columns, rows=rows, row_count=len(rows),
        truncated=truncated, max_rows=effective_max, duration_ms=duration_ms,
        tables=resolved_tables,
    )
# Phase 5 Step 5 — Safe executor & sanitized audit logging

## Components

- `app/database/executor.py` — `execute_sql()` runs generated SQL through the
  dedicated read-only role only (`get_readonly_connection(read_only=True)`). The
  admin connection is never imported or used. Guardrails: read-only transaction,
  `SET LOCAL` timeouts (statement ≤ 10s, lock 5s, idle-in-txn 30s), hard row cap
  `MAX_ROWS = 1000`, `fetchmany(max_rows + 1)` for accurate truncation, caller
  `max_rows` clamped to the cap, rollback + reliable close on every path, no silent
  retry. Returns `ExecutionResult(columns, rows, row_count, truncated, max_rows,
  duration_ms, tables, status)`. Database errors raise `ExecutionError` with a
  SQLSTATE-derived code (`read_only_transaction`, `permission_denied`,
  `statement_timeout`, `undefined_table`, `undefined_column`, `connection_error`, …).
- `app/security/audit.py` — logger `copilot.audit`, one JSON object per event.

## 1. Audit sink (how events reach a real sink)

By default `copilot.audit` has **only a `NullHandler`** and `propagate=True`.
**No persistent audit log is written by default.** Events propagate to the root
logger and are emitted only if the application configures a real handler. This is
stated explicitly — persistence is not claimed while only a NullHandler exists.

To route events to a real sink:

```python
from app.security import audit
import logging

# console (stderr) sink:
audit.configure_audit_logging()

# or a persistent file sink:
audit.configure_audit_logging(logging.FileHandler("audit.log", encoding="utf-8"))
```

`configure_audit_logging(handler=None, level=INFO)` is idempotent (the same handler
is never added twice). Alternatively, `logging.basicConfig(...)` at application
startup captures the propagated records. Wiring a production sink is a
deployment/Step 6 concern; the executor already emits every outcome.

## 2. SQL redaction & fingerprinting

- `redact_sql_literals()` replaces string/number literals with `?` **and removes
  comments** (`tree.sql(comments=False)`), so comment-borne secrets cannot leak.
  Identifiers/tables/columns/functions remain for debugging.
- Malformed or unparseable SQL returns `<unparseable>` (empty → `<empty>`); the raw
  SQL is **never** echoed on any fallback path.
- `sql_fingerprint` is computed over the **redacted** SQL, so it is **stable across
  literal variations** (structurally identical queries share a fingerprint) and
  never depends on sensitive values.

## 3. Exception sanitization

- `sanitize_text()` redacts known environment secret values plus patterns: URLs /
  connection strings, `Bearer` tokens, `sk-…` keys, and `key=value` secrets
  (including env-style names like `PG_RO_PASSWORD=`).
- `sanitize_error()` additionally redacts **quoted segments** (`"…"` / `'…'`) that
  PostgreSQL echoes in error details (offending values, host, user). Used for all
  executor error messages and audit `error_message` fields. Verified against real
  paths: `invalid input syntax for type integer: "value"` → value redacted;
  connection failure → host/user redacted, password never echoed by PostgreSQL.
- Raw result rows are never logged; only counts and metadata.

**Limitation:** redaction is pattern + known-value + quoted-segment based. A bare,
unquoted secret in an unusual format that is neither a configured environment
value nor matches a pattern could survive in free text. Mitigations: literals and
comments are stripped from logged SQL, quoted values are stripped from errors, and
results are never logged.

## 4. Transaction & timeout isolation

- `execute_sql` exposes **no `read_only` override**; the connection is always opened
  read-only. Even with `validate=False`, writes fail (read-only txn / role grants).
- `statement_timeout_ms` is clamped to ≤ 10000ms; a caller cannot weaken it.
- A **fresh connection per request** (verified: distinct `pg_backend_pid()` across
  calls); connections are closed by the context manager on success and failure.
- Both success and failure paths roll back, leaving no open transaction.

## Tests

- `tests/test_executor.py` — 23 offline unit tests (clamping, truncation, rollback,
  admin isolation, audit sanitization).
- `tests/test_executor_live.py` — 12 live integration tests.
- `tests/test_security_review.py` — 27 focused review tests (23 offline + 4 live).

```
python -m pytest -q -m "not live"                 # 251 passed, 54 deselected
python -m pytest tests/test_db_permissions_live.py tests/test_executor_live.py \
                 tests/test_security_review.py -q -m live   # 44 passed
```

No PostgreSQL privileges, application data, or role configuration were modified.
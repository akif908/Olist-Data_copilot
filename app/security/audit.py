"""Sanitized security audit logging (Phase 5, Step 5).

Records execution/validation outcomes for debugging and security review WITHOUT
ever exposing secrets or sensitive data:

* never logs passwords, API keys, connection strings, or tokens — known secret
  values from the environment and secret-like patterns are redacted;
* never logs raw query results or customer/payment row data — only counts and
  metadata;
* SQL is logged with literals AND comments removed (structure preserved for
  debugging); the fingerprint is computed over that redacted SQL so it is stable
  across literal variations and never depends on sensitive values;
* exception details are sanitized, including quoted literals/values that
  PostgreSQL may echo (e.g. ``invalid input syntax ... "value"``).

Sink behavior (IMPORTANT): the ``copilot.audit`` logger ships with only a
``NullHandler`` and ``propagate=True``. By default NO persistent audit log is
written — events propagate to the root logger and are emitted only if the
application configures a real handler. Call ``configure_audit_logging()`` (or
attach your own handler / ``logging.basicConfig``) to route events to a real sink.
This module does not silently claim persistence while only a NullHandler exists.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import sqlglot
from sqlglot import exp

AUDIT_LOGGER = logging.getLogger("copilot.audit")
AUDIT_LOGGER.addHandler(logging.NullHandler())


def configure_audit_logging(
    handler: logging.Handler | None = None,
    level: int = logging.INFO,
) -> logging.Handler:
    """Attach a real sink so audit events are actually emitted/persisted.

    Idempotent: the same handler is never added twice. With no argument, a
    ``StreamHandler`` writing the JSON message to stderr is attached. Applications
    that want a file sink pass ``logging.FileHandler(...)``. Until this is called
    (or the root logger is configured), only the default NullHandler exists and
    nothing is persisted.
    """
    AUDIT_LOGGER.setLevel(level)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
    if handler not in AUDIT_LOGGER.handlers:
        AUDIT_LOGGER.addHandler(handler)
    return handler

REDACTED = "<REDACTED>"

# Environment variables whose values must never appear in logs.
_SECRET_ENV_SUFFIXES = ("API_KEY", "PASSWORD", "SECRET", "TOKEN", "PASSWD")

# Secret-like patterns (defense-in-depth on top of known-value redaction).
# Order matters: URL / bearer / raw-key patterns run before key=value so a token
# following "Authorization: Bearer" is removed before the value is masked.
_PATTERNS = [
    # connection strings / URLs with embedded credentials
    (re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb|redis|amqp|https?)://[^\s\"']+"),
     "<REDACTED_URL>"),
    # bearer tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer " + REDACTED),
    # OpenAI/OpenRouter-style keys
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), REDACTED),
    # key=value / key: value secrets, including env-style names (PG_RO_PASSWORD=...)
    (re.compile(
        r"(?i)([A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_-]?key|authorization)"
        r"[A-Za-z0-9_]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"),
     r"\1\2" + REDACTED),
]


def _known_secrets() -> list[str]:
    """Collect non-trivial secret values from the environment (never logged)."""
    out: list[str] = []
    for key, val in os.environ.items():
        if key.upper().endswith(_SECRET_ENV_SUFFIXES) and val and len(val) >= 4:
            out.append(val)
    # Longest first so a secret containing another is fully redacted.
    return sorted(set(out), key=len, reverse=True)


def sanitize_text(text: str | None) -> str | None:
    """Redact known secret values and secret-like patterns from arbitrary text."""
    if text is None:
        return None
    out = str(text)
    for secret in _known_secrets():
        if secret in out:
            out = out.replace(secret, REDACTED)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


# Quoted segments PostgreSQL may echo in error details (offending values, host,
# user, identifiers). Redacted for error messages so no literal/data leaks; the
# audit record still carries sql_redacted + tables + error_code for debugging.
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")


def sanitize_error(text: str | None) -> str | None:
    """Sanitize an exception/error message, additionally redacting quoted values."""
    if text is None:
        return None
    out = sanitize_text(text) or ""
    return _QUOTED.sub(REDACTED, out)


def sql_fingerprint(sql: str) -> str:
    """Short stable hash of the (redacted) SQL for correlation."""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def redact_sql_literals(sql: str) -> str:
    """Return SQL with literals replaced by '?' AND comments removed.

    Identifiers, tables, columns and functions remain visible for debugging, but
    customer/payment literals and any comment-borne content are removed.
    Unparseable SQL is never echoed (returns a placeholder).
    """
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return "<unparseable>"
    if tree is None:
        return "<empty>"
    for lit in list(tree.find_all(exp.Literal)):
        lit.replace(exp.Literal.string("?"))
    return tree.sql(dialect="postgres", comments=False)


@dataclass(frozen=True)
class AuditRecord:
    timestamp: str
    event: str                 # "execution" | "validation"
    status: str                # "ok" | "rejected" | "error"
    sql_fingerprint: str
    sql_redacted: str
    tables: tuple[str, ...] = ()
    duration_ms: float | None = None
    row_count: int | None = None
    truncated: bool | None = None
    max_rows: int | None = None
    error_code: str | None = None
    error_message: str | None = None   # already sanitized

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


def _emit(record: AuditRecord) -> AuditRecord:
    AUDIT_LOGGER.info(record.to_json())
    return record


def audit_execution(
    *,
    sql: str,
    status: str,
    tables: tuple[str, ...] = (),
    duration_ms: float | None = None,
    row_count: int | None = None,
    truncated: bool | None = None,
    max_rows: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> AuditRecord:
    """Record a safe-executor outcome. Results/rows are NEVER logged."""
    redacted = sanitize_text(redact_sql_literals(sql)) or ""
    return _emit(AuditRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        event="execution",
        status=status,
        sql_fingerprint=sql_fingerprint(redacted),
        sql_redacted=redacted,
        tables=tuple(tables),
        duration_ms=duration_ms,
        row_count=row_count,
        truncated=truncated,
        max_rows=max_rows,
        error_code=error_code,
        error_message=sanitize_error(error_message),
    ))


def audit_validation(
    *,
    sql: str,
    status: str,
    tables: tuple[str, ...] = (),
    error_code: str | None = None,
    error_message: str | None = None,
) -> AuditRecord:
    """Record a validator outcome (rejection reason is sanitized)."""
    redacted = sanitize_text(redact_sql_literals(sql)) or ""
    return _emit(AuditRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        event="validation",
        status=status,
        sql_fingerprint=sql_fingerprint(redacted),
        sql_redacted=redacted,
        tables=tuple(tables),
        error_code=error_code,
        error_message=sanitize_error(error_message),
    ))
"""SQLGlot-based static SQL validation (Phase 5, security layer L2).

This is a *redundant early-rejection* layer. The real security boundary is the
dedicated read-only PostgreSQL role (L3), which independently prevents writes,
DDL, and access to ungranted tables even if this validator is bypassed.

Validation is structural (full AST inspection), never regex/string matching, so
comment, casing, whitespace, and quoting obfuscation do not bypass it:

* exactly one statement, and it must be a SELECT query (set operations allowed);
* no DML/DDL anywhere in the tree — including data-modifying CTEs, SELECT INTO,
  and row-locking clauses (FOR UPDATE/SHARE);
* every real table is in the approved allowlist and in the public schema; system
  catalogs (pg_catalog, information_schema) are rejected;
* every column resolves against the verified schema (SQLGlot qualification);
* functions are default-deny: only a curated allowlist passes, so dangerous
  functions (pg_read_file, pg_sleep, dblink, lo_import, set_config, ...) and
  table functions (generate_series, unnest, ROWS FROM) are rejected;
* schema-qualified and quoted function names are inspected, not just bare ones.

No query rewriting is performed. The ORIGINAL SQL string is returned for
execution; qualification is used for analysis only. Unsupported structures are
rejected rather than silently altered (refinement #4).
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from app.security.allowlist import SchemaAllowlist

# Rejection codes — distinguishable in tests, reports, and audit logs (refinement #6).
PARSE_ERROR = "parse_error"
EMPTY = "empty"
MULTIPLE_STATEMENTS = "multiple_statements"
NOT_SELECT = "not_select"
FORBIDDEN_NODE = "forbidden_node"
UNKNOWN_TABLE = "unknown_table"
CATALOG_ACCESS = "catalog_access"
UNKNOWN_COLUMN = "unknown_column"
DANGEROUS_FUNCTION = "dangerous_function"
TABLE_FUNCTION = "table_function"
VALIDATION_ERROR = "validation_error"


class SQLValidationError(Exception):
    """The SQL is not a safe, approved, single read-only SELECT."""

    def __init__(self, reason: str, code: str = VALIDATION_ERROR) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class ValidationResult:
    sql: str  # the ORIGINAL validated SQL (never rewritten)
    tables: tuple[str, ...]


# Only these root expression types are read-only queries.
ALLOWED_ROOT = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)

# Any of these anywhere in the AST (including nested/CTE) is forbidden.
FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Drop, exp.Create, exp.Alter, exp.TruncateTable,
    exp.Command, exp.Set, exp.Copy, exp.Grant, exp.Revoke,
    exp.Transaction, exp.Commit, exp.Rollback, exp.Use,
    exp.Into, exp.Lock, exp.Analyze,
    exp.Export, exp.Pragma,
)

# Default-deny function allowlist (canonical SQLGlot names). Connectors (and/or/
# not) are boolean structure, included so they are not mistaken for functions.
ALLOWED_FUNCTIONS = frozenset({
    # aggregates
    "count", "sum", "avg", "min", "max", "array_agg", "string_agg",
    "bool_and", "bool_or",
    # math
    "round", "abs", "ceil", "ceiling", "floor", "mod", "power", "sqrt",
    "greatest", "least", "sign", "trunc",
    # null handling
    "coalesce", "nullif", "ifnull", "nvl",
    # string
    "lower", "upper", "length", "char_length", "trim", "ltrim", "rtrim",
    "substring", "substr", "replace", "concat", "concat_ws", "left", "right",
    "initcap", "position", "strpos",
    # date/time
    "date_trunc", "timestamp_trunc", "extract", "date_part", "now",
    "current_timestamp", "current_date", "age",
    # casting / boolean structure
    "cast", "and", "or", "not", "xor",
})

# Explicit denylist (belt-and-suspenders; default-deny already rejects these).
DENY_FUNCTIONS = frozenset({
    "lo_import", "lo_export", "lo_get", "lo_put", "dblink", "dblink_exec",
    "set_config", "current_setting", "query_to_xml", "query_to_xmlschema",
    "database_to_xml", "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_terminate_backend", "pg_cancel_backend", "pg_read_file",
    "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "pg_advisory_lock",
})


def _function_name(node: exp.Func) -> str:
    """Canonical lowercased function name, handling qualified/quoted Anonymous."""
    if isinstance(node, exp.Anonymous):
        this = node.this
        if isinstance(this, str):
            return this.strip().lower()
        return this.sql(dialect="postgres").strip().lower()
    try:
        names = node.sql_names()
    except Exception:
        names = []
    return names[0].lower() if names else type(node).__name__.lower()


def _check_functions(root: exp.Expression) -> None:
    for node in root.walk():
        if not isinstance(node, exp.Func):
            continue
        name = _function_name(node)
        base = name.rsplit(".", 1)[-1]  # strip any schema qualifier
        if base in DENY_FUNCTIONS or base.startswith("pg_") or name not in ALLOWED_FUNCTIONS:
            raise SQLValidationError(
                f"function not allowed: {name}", DANGEROUS_FUNCTION
            )


def _check_table_functions(root: exp.Expression) -> None:
    for node in root.find_all(exp.Table):
        # A real table's `this` is an Identifier; a table function's `this` is a
        # function expression (generate_series, unnest, ROWS FROM, ...).
        if not isinstance(node.this, exp.Identifier):
            raise SQLValidationError(
                f"table function not allowed: {node.sql(dialect='postgres')}",
                TABLE_FUNCTION,
            )


def _check_forbidden(root: exp.Expression) -> None:
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SQLValidationError(
                f"forbidden construct: {type(node).__name__}", FORBIDDEN_NODE
            )


def _ident_value(ident: exp.Expression | None) -> str:
    """Effective identifier value with PostgreSQL case folding.

    Unquoted identifiers fold to lowercase (``OrDeRs`` -> ``orders``); quoted
    identifiers preserve case (``"ORDERS"`` stays ``ORDERS`` and will not match
    the lowercase allowlist). This matches both PostgreSQL and SQLGlot qualify().
    """
    if isinstance(ident, exp.Identifier):
        return ident.name if ident.quoted else ident.name.lower()
    return "" if ident is None else str(ident)


def _check_tables(root: exp.Expression, allowlist: SchemaAllowlist, cte_names: set[str]) -> None:
    for node in root.find_all(exp.Table):
        name = _ident_value(node.this)
        if name in cte_names:
            continue  # derived CTE relation; its underlying tables are checked too
        schema = _ident_value(node.args.get("db"))
        if schema not in allowlist.allowed_schemas:
            raise SQLValidationError(
                f"schema/catalog access not allowed: {schema}.{name}", CATALOG_ACCESS
            )
        if not allowlist.is_table_allowed(name):
            raise SQLValidationError(
                f"table not in allowlist: {name}", UNKNOWN_TABLE
            )


def validate_sql(sql: str, allowlist: SchemaAllowlist | None = None) -> ValidationResult:
    """Validate that ``sql`` is a single, safe, approved read-only SELECT.

    Returns the ORIGINAL SQL (never rewritten) plus the real tables it reads.
    Raises SQLValidationError with a specific ``code`` on any violation.
    """
    allowlist = allowlist or SchemaAllowlist.load()
    if not sql or not sql.strip():
        raise SQLValidationError("empty SQL", EMPTY)
    text = sql.strip()

    try:
        parsed = sqlglot.parse(text, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise SQLValidationError(f"SQL parse error: {exc}", PARSE_ERROR) from exc

    statements = [s for s in parsed if s is not None]
    if not statements:
        raise SQLValidationError("no statement parsed", EMPTY)
    if len(statements) != 1:
        raise SQLValidationError(
            f"expected a single statement, found {len(statements)}", MULTIPLE_STATEMENTS
        )
    root = statements[0]

    if not isinstance(root, ALLOWED_ROOT):
        raise SQLValidationError(
            f"only SELECT queries are allowed, got {type(root).__name__}", NOT_SELECT
        )

    # Structural inspection of the ENTIRE tree (nested queries, CTEs, ...).
    _check_forbidden(root)
    _check_table_functions(root)
    _check_functions(root)
    cte_names = set()
    for c in root.find_all(exp.CTE):
        alias = c.args.get("alias")
        if alias is not None and isinstance(alias.this, exp.Identifier):
            cte_names.add(_ident_value(alias.this))
        else:
            cte_names.add(c.alias_or_name.lower())
    _check_tables(root, allowlist, cte_names)

    # Column resolution against the verified schema (analysis only; the original
    # SQL string is what gets executed). Unresolvable columns => rejection.
    try:
        qualified = qualify(
            root,
            schema=allowlist.sqlglot_schema(),
            dialect="postgres",
            validate_qualify_columns=True,
        )
    except Exception as exc:  # OptimizeError on unknown/ambiguous columns
        raise SQLValidationError(
            f"column could not be resolved against the schema: {exc}", UNKNOWN_COLUMN
        ) from exc

    real_tables: set[str] = set()
    for node in qualified.find_all(exp.Table):
        name = _ident_value(node.this)
        if name in cte_names:
            continue
        real_tables.add(name)
    for col in qualified.find_all(exp.Column):
        tbl = _ident_value(col.args.get("table"))
        cname = _ident_value(col.this)
        if tbl in allowlist.tables and not allowlist.is_column_allowed(tbl, cname):
            raise SQLValidationError(
                f"column not in allowlist: {tbl}.{cname}", UNKNOWN_COLUMN
            )

    return ValidationResult(sql=text, tables=tuple(sorted(real_tables)))
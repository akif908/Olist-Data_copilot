"""Phase 5 — SQL validation and safe execution security layer.

Modules:
    allowlist      approved schema/table/column allowlist from verified metadata
    sql_validator  SQLGlot AST validation (SELECT-only, allowlists, functions)
    audit          sanitized security audit logging
"""
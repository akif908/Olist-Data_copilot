"""Idempotent setup of the dedicated read-only role (Phase 5, Step 3).

SAFETY:
* Default mode is --dry-run: it prints the exact SQL (password REDACTED) and
  executes NOTHING that changes privileges.
* --execute applies the statements. It refuses to run without PG_RO_PASSWORD.
* The only database-wide change is REVOKE TEMPORARY ... FROM PUBLIC; everything
  else is role-specific. No table data is touched.
* The seven granted tables come from the verified metadata allowlist (single
  source of truth), not a hard-coded list.

Usage:
    python -m scripts.setup_readonly_user            # dry-run (default)
    python -m scripts.setup_readonly_user --execute  # apply (requires approval)
"""
from __future__ import annotations

import argparse
import sys

from psycopg import sql

from app.config import DatabaseSettings, ReadOnlyDatabaseSettings
from app.database.connection import get_connection
from app.security.allowlist import SchemaAllowlist

ROLE_ATTRS = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"


def build_statements(conn, ro: ReadOnlyDatabaseSettings, tables: list[str], role_exists: bool):
    """Return (description, Composed-or-str SQL, redacted_display) triples.

    The password is quoted with psycopg.sql.Literal (never string-interpolated).
    The redacted display is what dry-run prints, so the secret is never shown.
    """
    role_ident = sql.Identifier(ro.user)
    db_ident = sql.Identifier(ro.dbname)
    pw_literal = sql.Literal(ro.password)

    if role_exists:
        role_stmt = sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {} " + ROLE_ATTRS).format(
            role_ident, pw_literal
        )
        role_redacted = sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD '<REDACTED>' " + ROLE_ATTRS).format(
            role_ident
        )
        role_desc = f"ALTER existing role {ro.user} to the safe read-only state"
    else:
        role_stmt = sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} " + ROLE_ATTRS).format(
            role_ident, pw_literal
        )
        role_redacted = sql.SQL("CREATE ROLE {} LOGIN PASSWORD '<REDACTED>' " + ROLE_ATTRS).format(
            role_ident
        )
        role_desc = f"CREATE role {ro.user} (no dangerous attributes)"

    table_list = sql.SQL(", ").join(sql.Identifier(t) for t in tables)

    stmts = [
        (role_desc, role_stmt, role_redacted.as_string(conn)),
        (
            "GRANT CONNECT on the database",
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db_ident, role_ident),
            None,
        ),
        (
            "REVOKE TEMPORARY from PUBLIC (database-wide; blocks temp tables)",
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(db_ident),
            None,
        ),
        (
            "GRANT USAGE on schema public",
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role_ident),
            None,
        ),
        (
            f"GRANT SELECT on the {len(tables)} verified tables",
            sql.SQL("GRANT SELECT ON {} TO {}").format(table_list, role_ident),
            None,
        ),
        (
            "Role default: read-only transactions",
            sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role_ident),
            None,
        ),
        (
            "Role default: statement_timeout 10s",
            sql.SQL("ALTER ROLE {} SET statement_timeout = '10s'").format(role_ident),
            None,
        ),
        (
            "Role default: idle_in_transaction_session_timeout 30s",
            sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = '30s'").format(role_ident),
            None,
        ),
        (
            "Role default: lock_timeout 5s",
            sql.SQL("ALTER ROLE {} SET lock_timeout = '5s'").format(role_ident),
            None,
        ),
    ]
    out = []
    for desc, stmt, redacted in stmts:
        display = redacted if redacted is not None else stmt.as_string(conn)
        out.append((desc, stmt, display))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="apply the privilege changes (default is dry-run, which changes nothing)",
    )
    args = parser.parse_args()

    ro = ReadOnlyDatabaseSettings.from_env()
    admin = DatabaseSettings.from_env()
    if not ro.password:
        print("ERROR: PG_RO_PASSWORD is not set in the environment/.env.", file=sys.stderr)
        print("Set a strong password for the read-only role before running.", file=sys.stderr)
        return 2
    if ro.user == admin.user:
        print("ERROR: PG_RO_USER must differ from the admin PGUSER (refinement #9).", file=sys.stderr)
        return 2

    tables = sorted(SchemaAllowlist.load().tables)

    # Admin connection is used ONLY for setup. Existence check is read-only.
    with get_connection(admin) as conn:
        role_exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (ro.user,)
        ).fetchone() is not None

        statements = build_statements(conn, ro, tables, role_exists)

        mode = "EXECUTE" if args.execute else "DRY-RUN (no changes)"
        print(f"=== Read-only role setup — {mode} ===")
        print(f"Role: {ro.user}   Database: {ro.dbname}   Exists already: {role_exists}")
        print(f"Tables granted SELECT ({len(tables)}): {', '.join(tables)}")
        print()
        for i, (desc, _stmt, display) in enumerate(statements, 1):
            print(f"-- {i}. {desc}")
            print(display + ";")
            print()

        if not args.execute:
            print("Dry-run only. Re-run with --execute to apply (requires approval).")
            return 0

        print("Applying statements...")
        for desc, stmt, _display in statements:
            conn.execute(stmt)
            print(f"  OK: {desc}")
        conn.commit()
        print("Committed.")

        # Read-only verification of the resulting state.
        print("\n=== Verification (read-only) ===")
        print("role attrs:", conn.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolcanlogin "
            "FROM pg_roles WHERE rolname = %s", (ro.user,)
        ).fetchone())
        print("TEMP via PUBLIC:", conn.execute(
            "SELECT has_database_privilege('public', current_database(), 'TEMPORARY')"
        ).fetchone()[0])
        for t in tables:
            can_select = conn.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')", (ro.user, t)
            ).fetchone()[0]
            can_insert = conn.execute(
                "SELECT has_table_privilege(%s, %s, 'INSERT')", (ro.user, t)
            ).fetchone()[0]
            print(f"  {t:32} SELECT={can_select} INSERT={can_insert}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
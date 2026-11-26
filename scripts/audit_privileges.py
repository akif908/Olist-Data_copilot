"""Read-only audit of existing PostgreSQL privileges (Phase 5, Step 2).

Runs ONLY catalog SELECTs in a read-only transaction. It changes nothing. The
purpose is to understand the real isolation state before creating the dedicated
read-only role (refinement #1): direct grants alone do not establish isolation —
role memberships, inherited privileges, PUBLIC grants, ownership, TEMP and
default privileges all matter.

Usage:
    python -m scripts.audit_privileges            # print report
    python -m scripts.audit_privileges --save     # also write docs/privilege_audit.md
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from app.config import PROJECT_ROOT
from app.database.connection import get_connection

# Each entry: (title, sql). All are read-only catalog queries.
AUDIT_QUERIES: list[tuple[str, str]] = [
    (
        "Current connection identity",
        "SELECT current_user, session_user, "
        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser",
    ),
    (
        "Roles and role-level attributes",
        "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
        "rolbypassrls, rolcanlogin FROM pg_roles ORDER BY rolname",
    ),
    (
        "Role memberships (inheritance paths)",
        "SELECT r.rolname AS member, g.rolname AS granted_role, m.admin_option "
        "FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
        "JOIN pg_roles g ON g.oid = m.roleid ORDER BY member, granted_role",
    ),
    (
        "Database ownership and ACL (CONNECT / TEMPORARY)",
        "SELECT datname, pg_get_userbyid(datdba) AS owner, datacl "
        "FROM pg_database WHERE datname = current_database()",
    ),
    (
        "Schema ownership and ACL (public)",
        "SELECT nspname, pg_get_userbyid(nspowner) AS owner, nspacl "
        "FROM pg_namespace WHERE nspname = 'public'",
    ),
    (
        "Table ownership (public)",
        "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public' "
        "ORDER BY tablename",
    ),
    (
        "Table ACLs (public) — look for PUBLIC ('=') grants",
        "SELECT c.relname, c.relacl FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname",
    ),
    (
        "Default privileges (future objects)",
        "SELECT pg_get_userbyid(defaclrole) AS role, defaclobjtype, defaclacl "
        "FROM pg_default_acl",
    ),
    (
        "Existing copilot_ro role (if any)",
        "SELECT rolname, rolsuper, rolcanlogin, rolcreaterole, rolcreatedb "
        "FROM pg_roles WHERE rolname = 'copilot_ro'",
    ),
]


def run_audit() -> list[tuple[str, list[str], list[tuple]]]:
    out: list[tuple[str, list[str], list[tuple]]] = []
    with get_connection() as conn:
        conn.read_only = True
        conn.execute("SET statement_timeout = '30s'")
        for title, sql in AUDIT_QUERIES:
            cur = conn.execute(sql)
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
            out.append((title, cols, rows))
    return out


def format_report(results: list[tuple[str, list[str], list[tuple]]]) -> str:
    lines = [
        "# PostgreSQL privilege audit (read-only)",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "All queries are catalog SELECTs run in a read-only transaction; nothing "
        "was modified. This captures the isolation state before creating copilot_ro.",
        "",
    ]
    for title, cols, rows in results:
        lines.append(f"## {title}")
        lines.append("")
        if not rows:
            lines.append("_(no rows)_")
            lines.append("")
            continue
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "|".join(["---"] * len(cols)) + "|")
        for row in rows:
            cells = ["" if v is None else str(v).replace("|", "\\|").replace("\n", " ") for v in row]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true", help="write docs/privilege_audit.md")
    args = parser.parse_args()
    results = run_audit()
    report = format_report(results)
    print(report)
    if args.save:
        dest = PROJECT_ROOT / "docs" / "privilege_audit.md"
        dest.write_text(report, encoding="utf-8")
        print(f"\nSaved: {dest}")


if __name__ == "__main__":
    main()
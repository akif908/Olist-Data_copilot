"""Execute every SQL example and record its real output.

"Verified" means: the SQL actually ran against olist_copilot in a READ ONLY
transaction and the recorded result is what the database returned. Outputs
are written to knowledge_base/sql_examples/verified_results.json and rendered
to docs/sql_examples.md.

Usage:
    python -m scripts.verify_sql_examples
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import PROJECT_ROOT
from app.database.connection import get_connection

EXAMPLES_PATH = PROJECT_ROOT / "knowledge_base" / "sql_examples" / "examples.json"
RESULTS_PATH = PROJECT_ROOT / "knowledge_base" / "sql_examples" / "verified_results.json"
REPORT_PATH = PROJECT_ROOT / "docs" / "sql_examples.md"

MAX_ROWS_RECORDED = 25


def load_examples(path: Path = EXAMPLES_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["examples"]


def check_select_only(sql: str) -> None:
    """Minimal guard until the full SQLGlot validator arrives in Phase 5."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped.lower().startswith("select"):
        raise ValueError("example SQL must be a SELECT statement")
    if ";" in stripped:
        raise ValueError("example SQL must be a single statement")


def verify_example(conn, example: dict) -> dict:
    check_select_only(example["sql"])
    start = time.perf_counter()
    cur = conn.execute(example["sql"])
    rows = cur.fetchall()
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    columns = [d.name for d in cur.description]
    recorded_rows = [list(r) for r in rows[:MAX_ROWS_RECORDED]]
    return {
        "id": example["id"],
        "columns": columns,
        "row_count": len(rows),
        "rows": recorded_rows,
        "rows_truncated": len(rows) > MAX_ROWS_RECORDED,
        "scalar": rows[0][0] if len(rows) == 1 and len(columns) == 1 else None,
        "duration_ms": duration_ms,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def render_report(examples: list[dict], results: dict[str, dict]) -> str:
    lines = [
        "# Verified SQL Example Library",
        "",
        "Every example below was executed against `olist_copilot` in a read-only",
        "transaction by `python -m scripts.verify_sql_examples`. Results are the",
        "actual database output, not estimates. Re-run the script to refresh.",
        "",
    ]
    for ex in examples:
        r = results[ex["id"]]
        lines += [f"## {ex['id']} — {ex['question']}", "",
                  f"```sql\n{ex['sql']}\n```", ""]
        if ex.get("notes"):
            lines += [f"*{ex['notes']}*", ""]
        if r["scalar"] is not None:
            lines += [f"**Result:** `{r['scalar']}`", ""]
        else:
            lines += ["| " + " | ".join(r["columns"]) + " |",
                      "|" + "---|" * len(r["columns"])]
            for row in r["rows"]:
                lines.append("| " + " | ".join(str(v) for v in row) + " |")
            if r["rows_truncated"]:
                lines.append(f"*… {r['row_count']} rows total, first {MAX_ROWS_RECORDED} shown.*")
            lines.append("")
        lines += [f"*{r['row_count']} row(s) returned in {r['duration_ms']} ms.*", ""]
    return "\n".join(lines)


def main() -> None:
    examples = load_examples()
    results: dict[str, dict] = {}
    with get_connection() as conn:
        conn.read_only = True  # defense in depth; full enforcement in Phase 5
        for ex in examples:
            results[ex["id"]] = verify_example(conn, ex)
            r = results[ex["id"]]
            shown = f"scalar={r['scalar']}" if r["scalar"] is not None else f"{r['row_count']} rows"
            print(f"VERIFIED {ex['id']}: {shown} ({r['duration_ms']} ms)")

    RESULTS_PATH.write_text(
        json.dumps({"results": results}, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    REPORT_PATH.write_text(render_report(examples, results), encoding="utf-8")
    print(f"\n{len(results)} examples verified.")
    print(f"Results: {RESULTS_PATH}")
    print(f"Report:  {REPORT_PATH}")


if __name__ == "__main__":
    main()
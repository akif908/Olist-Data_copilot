"""Ask the copilot a natural-language question and display the generated SQL.

Phase 3 behavior: the SQL is DISPLAYED, not executed automatically. The
--execute flag is a developer convenience that runs the query in a read-only
transaction with a statement timeout; the real safe executor arrives in
Phase 5.

Phase 4: --rag grounds generation in retrieved knowledge (top-k chunks) instead
of the full knowledge base, and prints the retrieved sources for inspection.

Usage:
    python -m scripts.ask "How many orders are in the database?"
    python -m scripts.ask "Top five categories by revenue?" --execute
    python -m scripts.ask "What is the average order value?" --rag
    python -m scripts.ask --list-models        # free OpenRouter models
"""
from __future__ import annotations

import argparse

import httpx

from app.agent.sql_generator import generate_sql
from app.database.connection import get_connection
from app.llm.base import LLMError
from app.llm.provider_factory import create_llm_service
from app.rag.retriever import RetrievalError, Retriever


def list_free_models() -> None:
    resp = httpx.get("https://openrouter.ai/api/v1/models", timeout=30)
    resp.raise_for_status()
    for m in resp.json()["data"]:
        p = m.get("pricing", {})
        if p.get("prompt") == "0" and p.get("completion") == "0":
            print(f"{m['id']}  (ctx {m.get('context_length')})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="*", help="natural-language question")
    parser.add_argument("--execute", action="store_true",
                        help="DEV ONLY: run the SQL read-only (Phase 5 adds the safe executor)")
    parser.add_argument("--max-rows", type=int, default=20)
    parser.add_argument("--rag", action="store_true",
                        help="ground generation in retrieved knowledge (Phase 4)")
    parser.add_argument("--k", type=int, default=8,
                        help="number of knowledge chunks to retrieve with --rag")
    parser.add_argument("--list-models", action="store_true",
                        help="list free OpenRouter models and exit")
    args = parser.parse_args()

    if args.list_models:
        list_free_models()
        return
    if not args.question:
        parser.error("a question is required (or use --list-models)")

    question = " ".join(args.question)
    try:
        llm = create_llm_service()
        retriever = Retriever() if args.rag else None
        result = generate_sql(question, llm, retriever=retriever, k=args.k)
    except LLMError as exc:
        raise SystemExit(f"LLM error: {exc}")
    except RetrievalError as exc:
        raise SystemExit(f"Retrieval error: {exc}")

    print(f"Question: {question}")
    print(f"Model: {result.model} | tokens: {result.prompt_tokens} in / "
          f"{result.completion_tokens} out")
    if args.rag:
        print(f"RAG: {len(result.retrieved_sources)} retrieved sources")
        for src in result.retrieved_sources:
            print(f"  - {src}")
    print("-" * 70)
    if not result.answerable:
        print("NOT ANSWERABLE from this database.")
        print(f"Reason: {result.unsupported_reason}")
        return

    print(f"Explanation: {result.explanation}")
    print(f"Tables used: {', '.join(result.tables_used)}")
    if result.metrics_used:
        print(f"Metrics applied: {', '.join(result.metrics_used)}")
    print("\nGenerated SQL (review before execution):\n")
    print(result.sql)

    if args.execute:
        print("\n--- DEV-ONLY read-only execution ---")
        with get_connection() as conn:
            conn.read_only = True
            conn.execute("SET statement_timeout = '10s'")
            cur = conn.execute(result.sql)
            cols = [d.name for d in cur.description]
            rows = cur.fetchmany(args.max_rows)
            print(" | ".join(cols))
            for row in rows:
                print(" | ".join(str(v) for v in row))
            print(f"({len(rows)} row(s) shown, max {args.max_rows})")


if __name__ == "__main__":
    main()
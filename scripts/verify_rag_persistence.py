"""Verify the RAG index persists across process restarts (Phase 4).

Opens the persisted ChromaDB store in THIS fresh process — without re-ingesting —
and confirms the chunk count, the stored embedding-model metadata, and that a few
known questions retrieve their expected sources. Run after ingest_knowledge.

Usage:
    python -m scripts.verify_rag_persistence
    python -m scripts.verify_rag_persistence --collection olist_knowledge_local
"""
from __future__ import annotations

import argparse

from app.rag.ingestion import CHROMA_DIR, COLLECTION_NAME
from app.rag.retriever import RetrievalError, Retriever

# question -> a source label that MUST appear in the top-k results
EXPECTATIONS = {
    "What is the average order value?": "metric:average_order_value",
    "How many unique customers exist?": "metric:customer_count",
    "What is the cancellation rate?": "metric:cancellation_rate",
    "What is our net profit?": "unsupported_metric:profit",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--persist-dir", default=str(CHROMA_DIR))
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    try:
        retriever = Retriever(
            persist_dir=args.persist_dir, collection_name=args.collection
        )
    except RetrievalError as exc:
        raise SystemExit(f"Persistence check failed: {exc}")

    print(f"Reopened persisted collection '{retriever.collection_name}' (no re-ingest)")
    print(f"  provider : {retriever.provider}")
    print(f"  model    : {retriever.model}")
    print(f"  chunks   : {retriever.count()}")
    print("-" * 60)

    failures = 0
    for question, expected in EXPECTATIONS.items():
        chunks = retriever.retrieve(question, k=args.k)
        labels = [c.label() for c in chunks]
        ok = expected in labels
        failures += 0 if ok else 1
        status = "OK" if ok else "MISS"
        print(f"[{status}] {question}")
        print(f"       expected {expected} in top-{args.k}: {labels}")

    print("-" * 60)
    if failures:
        raise SystemExit(f"Persistence check: {failures} expectation(s) failed")
    print("Persistence check PASSED — index survived restart and retrieves correctly")


if __name__ == "__main__":
    main()
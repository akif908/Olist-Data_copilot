"""Build/refresh the persisted RAG knowledge index (Phase 4).

Embeds the verified knowledge base (schema, glossary, SQL examples) and stores it
in ChromaDB under chroma_db/. The embedding model is taken from configuration
(EMBEDDING_PROVIDER / <PROVIDER>_EMBEDDING_MODEL); it is never silently swapped.

Usage:
    python -m scripts.ingest_knowledge                 # idempotent refresh
    python -m scripts.ingest_knowledge --reset         # rebuild from scratch
    python -m scripts.ingest_knowledge --provider local --collection olist_knowledge_local
"""
from __future__ import annotations

import argparse

from app.config import EmbeddingSettings
from app.rag.embedding_factory import create_embedding_service
from app.rag.embeddings import EmbeddingError
from app.rag.ingestion import CHROMA_DIR, COLLECTION_NAME, IngestionError, ingest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="delete and rebuild the collection (required to switch models)")
    parser.add_argument("--provider", help="override EMBEDDING_PROVIDER (openrouter|local)")
    parser.add_argument("--model", help="override the embedding model id")
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--persist-dir", default=str(CHROMA_DIR))
    args = parser.parse_args()

    settings = EmbeddingSettings.from_env()
    if args.provider or args.model:
        settings = EmbeddingSettings(
            provider=args.provider or settings.provider,
            api_key=settings.api_key,
            model=args.model or settings.model,
            base_url=settings.base_url,
        )

    try:
        svc = create_embedding_service(settings)
        result = ingest(
            embedding_service=svc,
            persist_dir=args.persist_dir,
            collection_name=args.collection,
            reset=args.reset,
        )
    except (EmbeddingError, IngestionError) as exc:
        raise SystemExit(f"Ingestion error: {exc}")

    print("RAG knowledge index built")
    print(f"  collection : {result.collection}")
    print(f"  persist_dir: {result.persist_dir}")
    print(f"  provider   : {result.provider}")
    print(f"  model      : {result.model}")
    print(f"  dimension  : {result.dimension}")
    print(f"  chunks     : {result.chunk_count}")
    for t, n in sorted(result.by_type.items()):
        print(f"    - {t}: {n}")


if __name__ == "__main__":
    main()
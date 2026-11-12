"""Phase 4 — Retrieval-Augmented Generation over the verified knowledge base.

Modules:
    embeddings  provider-independent embedding services (OpenRouter + local)
    ingestion   chunk the knowledge base and persist vectors in ChromaDB
    retriever   semantic retrieval of relevant, inspectable knowledge chunks
"""
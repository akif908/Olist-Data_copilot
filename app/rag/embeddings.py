"""Provider-independent text embeddings.

The application depends only on the EmbeddingService interface. Two
implementations ship:

* OpenRouterEmbeddingService — OpenAI-compatible /embeddings over plain HTTP
  (httpx), defaulting to the user-selected ``nvidia/nemotron-3-embed-1b:free``.
* LocalSentenceTransformerEmbedding — an optional local baseline used only for
  retrieval comparison; requires the separate requirements-baseline.txt.

Design rules (Phase 4):
* The model is configurable and is NEVER silently swapped for another one.
  If the configured model fails, we raise EmbeddingError — we do not fall back.
* The embedding dimension is discovered from the real API response, not
  assumed, so a different configured model still works.
* Credentials are read from configuration and never logged or placed in error
  messages.
* Free endpoints are rate-limited: transient 429/5xx/transport failures are
  retried with exponential backoff (honouring Retry-After).
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_EMBEDDING_MODEL = "nvidia/nemotron-3-embed-1b:free"
DEFAULT_LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
APP_TITLE = "Data Analyst Copilot"


class EmbeddingError(Exception):
    """Embedding configuration, transport, or response failure (sanitized)."""


class EmbeddingService(ABC):
    """Minimal embedding interface used by ingestion and retrieval."""

    provider: str = "abstract"

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable model identifier (stored with the vectors to detect changes)."""
        raise NotImplementedError

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Length of each embedding vector, verified against a real response."""
        raise NotImplementedError

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, preserving input order. Must not leak keys."""
        raise NotImplementedError

    def embed_one(self, text: str) -> list[float]:
        """Convenience wrapper for a single text."""
        return self.embed([text])[0]


class OpenRouterEmbeddingService(EmbeddingService):
    """OpenRouter embeddings via the OpenAI-compatible /embeddings endpoint."""

    provider = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_EMBEDDING_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        batch_size: int = 32,
        max_retries: int = 4,
    ) -> None:
        if not api_key:
            raise EmbeddingError(
                "OPENROUTER_API_KEY is not set. Add it to .env (see .env.example)."
            )
        if batch_size < 1:
            raise EmbeddingError("batch_size must be >= 1")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._batch_size = batch_size
        self._max_retries = max(1, max_retries)
        self._dimension: int | None = None

    @property
    def name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        # Discover the real dimension from a probe if no batch has run yet.
        if self._dimension is None:
            self.embed(["dimension probe"])
        assert self._dimension is not None
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start:start + self._batch_size]
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": batch}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Title": APP_TITLE,
        }
        resp = self._post_with_retry(payload, headers)
        if resp.status_code != 200:
            raise EmbeddingError(self._sanitize_error(resp))

        try:
            data = resp.json()
            items = data["data"]
            # The API may return items out of order; sort by index to match input.
            ordered = sorted(items, key=lambda d: d["index"])
            vectors = [item["embedding"] for item in ordered]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise EmbeddingError(
                "OpenRouter embeddings returned an unexpected response shape"
            ) from exc

        if len(vectors) != len(batch):
            raise EmbeddingError(
                f"OpenRouter returned {len(vectors)} embeddings for {len(batch)} inputs"
            )
        dims = {len(v) for v in vectors}
        if len(dims) != 1:
            raise EmbeddingError("OpenRouter returned inconsistent embedding dimensions")
        dim = dims.pop()
        if dim == 0:
            raise EmbeddingError("OpenRouter returned an empty embedding vector")
        if self._dimension is None:
            self._dimension = dim
        elif dim != self._dimension:
            # A dimension change mid-run means the model behaviour changed; do
            # not silently accept it (vectors would be incomparable).
            raise EmbeddingError(
                f"embedding dimension changed from {self._dimension} to {dim}"
            )
        return vectors

    def _post_with_retry(self, payload: dict, headers: dict) -> httpx.Response:
        """POST with backoff on transient failures. Never switches model."""
        last_resp: httpx.Response | None = None
        for attempt in range(self._max_retries):
            try:
                resp = httpx.post(
                    f"{self._base_url}/embeddings",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                    # Ignore environment proxy settings: some Windows setups have
                    # NO_PROXY entries (e.g. bare ::1) that crash httpx's URL
                    # parser. Connect directly (same fix as the chat service).
                    trust_env=False,
                )
            except httpx.HTTPError as exc:
                if attempt == self._max_retries - 1:
                    raise EmbeddingError(
                        f"OpenRouter embeddings request failed: {type(exc).__name__}"
                    ) from exc
                time.sleep(self._backoff(attempt))
                continue

            last_resp = resp
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries - 1:
                    break
                time.sleep(self._retry_delay(resp, attempt))
                continue
            # 2xx or a non-retryable 4xx (e.g. 400/401/404): return as-is.
            return resp
        assert last_resp is not None
        return last_resp

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0 * (2 ** attempt), 30.0)

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(float(retry_after), self._backoff(attempt)), 60.0)
            except ValueError:
                pass
        return self._backoff(attempt)

    @staticmethod
    def _sanitize_error(resp: httpx.Response) -> str:
        """Status + provider error message only. Never includes the API key."""
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        detail = (detail or resp.text or "")[:300]
        return f"OpenRouter embeddings API error {resp.status_code}: {detail}"


class LocalSentenceTransformerEmbedding(EmbeddingService):
    """Optional local baseline (sentence-transformers). Not used in production.

    Kept behind a lazy import so the core app never requires torch. Install with
    ``pip install -r requirements-baseline.txt`` to run the comparison.
    """

    provider = "local"

    def __init__(self, model_name: str = DEFAULT_LOCAL_MODEL) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise EmbeddingError(
                "sentence-transformers is not installed. Install the optional "
                "baseline with: pip install -r requirements-baseline.txt"
            ) from exc
        self._model_name = model_name
        self._model = SentenceTransformer(model_name)
        self._dimension = int(self._model.get_sentence_embedding_dimension())

    @property
    def name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        arr = self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return arr.tolist()
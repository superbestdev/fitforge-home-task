"""Embedding client.

Talks to Ollama's native embedding endpoint. Kept deliberately small and
HTTP-only: no torch, no sentence-transformers, no model weights inside the app
image. The embedding server is a swappable dependency behind a URL, same as the
chat model.
"""

from __future__ import annotations

import logging
import time

import httpx

from services.api.app.config import settings

log = logging.getLogger(__name__)

# Ollama holds the whole batch in memory and embeds serially on CPU; large
# batches gain nothing and risk a timeout.
BATCH_SIZE = 16


class EmbeddingError(RuntimeError):
    pass


def embed_texts(texts: list[str], *, retries: int = 3) -> list[list[float]]:
    """Embed a list of texts, preserving order."""
    if not texts:
        return []

    vectors: list[list[float]] = []
    with httpx.Client(base_url=settings.embed_base_url, timeout=180.0) as client:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start:start + BATCH_SIZE]
            vectors.extend(_embed_batch(client, batch, retries))
    return vectors


def _embed_batch(client: httpx.Client, batch: list[str], retries: int) -> list[list[float]]:
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            resp = client.post(
                "/api/embed",
                json={"model": settings.embed_model, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            vectors = data.get("embeddings")
            if not vectors or len(vectors) != len(batch):
                raise EmbeddingError(
                    f"expected {len(batch)} embeddings, got {len(vectors or [])}"
                )
            _check_dim(vectors[0])
            return vectors
        except Exception as exc:                     # noqa: BLE001 - retried below
            last_error = exc
            wait = 2 ** attempt
            log.warning("embedding batch failed (attempt %d/%d): %s; retrying in %ds",
                        attempt + 1, retries, exc, wait)
            time.sleep(wait)

    raise EmbeddingError(f"embedding failed after {retries} attempts: {last_error}")


def _check_dim(vector: list[float]) -> None:
    """Fail loudly on a dimension mismatch.

    Changing EMBED_MODEL without changing EMBED_DIM (and the vector column) is
    an easy mistake that otherwise surfaces as silently terrible retrieval.
    """
    if len(vector) != settings.embed_dim:
        raise EmbeddingError(
            f"embedding model '{settings.embed_model}' returned dim {len(vector)}, "
            f"but EMBED_DIM is {settings.embed_dim}. Update EMBED_DIM and the "
            f"doc_chunks.embedding column type, then re-ingest."
        )


def embed_query(text: str) -> list[float]:
    """Embed a single search query."""
    return embed_texts([text])[0]


def healthy() -> bool:
    try:
        with httpx.Client(base_url=settings.embed_base_url, timeout=10.0) as client:
            return client.get("/api/tags").status_code == 200
    except Exception:                                 # pragma: no cover
        return False

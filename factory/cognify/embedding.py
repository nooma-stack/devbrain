"""Shared embedding helper for cognify/curator memory writers.

Several memory-writing paths (extract atoms, curator lesson candidates,
fan-out summaries) need to embed the row's text so `deep_search` (which
filters `embedding IS NOT NULL`) can find it. They previously each either
duplicated the Ollama call or — for atom inserts — skipped embedding
entirely, which left ~22k decision/pattern atoms invisible to search
(observed 2026-06-26). This is the single source of truth.

`embed_text` degrades gracefully: if Ollama is unreachable it returns None
so the caller still writes the row (sans vector), and a later reembed pass
(`scripts/reembed_memory.py`) backfills the vector.
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


def _ollama_config() -> tuple[str, str]:
    # Resolve at call time so test envs can monkeypatch / override via env.
    try:
        from ingest.config import EMBED_MODEL, OLLAMA_URL  # noqa: PLC0415

        return OLLAMA_URL, EMBED_MODEL
    except ImportError:
        import os  # noqa: PLC0415

        return (
            os.environ.get("DEVBRAIN_OLLAMA_URL", "http://localhost:11434"),
            os.environ.get("DEVBRAIN_EMBEDDING_MODEL", "snowflake-arctic-embed2"),
        )


def embed_text(text: str) -> list[float] | None:
    """Embed `text` via Ollama. Returns the vector, or None if Ollama is
    unreachable / returns nothing (caller writes the row without an
    embedding; a reembed pass can backfill it later)."""
    if not text:
        return None
    try:
        ollama_url, model = _ollama_config()
        data = json.dumps({"model": model, "input": text}).encode()
        req = urllib.request.Request(
            f"{ollama_url}/api/embed",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
        emb = payload.get("embeddings", [None])[0]
        return emb or None
    except Exception as exc:  # noqa: BLE001 — graceful: row still written
        logger.warning("embed_text: Ollama embed failed (%s); row written sans vector", exc)
        return None


def to_vector_literal(embedding: list[float]) -> str:
    """Format a vector for a pgvector `%s::vector` parameter."""
    return "[" + ",".join(str(x) for x in embedding) + "]"

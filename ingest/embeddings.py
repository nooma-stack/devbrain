"""Embedding helpers using local Ollama.

Documents vs queries
--------------------
snowflake-arctic-embed2 is an asymmetric retrieval model: it was trained
with search QUERIES prefixed ``"query: "`` and stored DOCUMENTS embedded
bare. So there are two entry points here:

* ``embed`` / ``embed_batch`` — DOCUMENT embeddings. Used by every write
  path (ingest pipeline, store, reembed, codebase indexer, drift checks).
  These must never carry the query prefix: the corpus was embedded bare,
  and a re-embed that added it would silently shift every stored vector.
* ``embed_query`` — QUERY embeddings for search-time callers (the recall
  audit; any future Python search path). Prepends ``EMBED_QUERY_PREFIX``
  (env ``DEVBRAIN_QUERY_PREFIX`` > yaml ``embedding.query_prefix`` >
  model default; empty string disables).

The MCP server's deep_search mirrors this split in mcp-server/src/embeddings.ts.
"""

from __future__ import annotations

import json
import urllib.request

from config import EMBED_MODEL, EMBED_QUERY_PREFIX, OLLAMA_URL


def _post_embed(inp: str | list[str]) -> list[list[float]]:
    data = json.dumps({"model": EMBED_MODEL, "input": inp}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result["embeddings"]


def embed(text: str) -> list[float]:
    """Embed a DOCUMENT (no query prefix)."""
    return _post_embed(text)[0]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed DOCUMENTS in one request (no query prefix)."""
    return _post_embed(texts)


def query_text(text: str, prefix: str | None = None) -> str:
    """The exact string sent to the model for a search query.

    ``prefix=None`` uses the configured ``EMBED_QUERY_PREFIX``; pass ``""``
    to embed the raw query (e.g. for before/after measurement).
    """
    p = EMBED_QUERY_PREFIX if prefix is None else prefix
    return f"{p}{text}"


def embed_query(text: str, prefix: str | None = None) -> list[float]:
    """Embed a search QUERY (with the model's query prefix)."""
    return _post_embed(query_text(text, prefix))[0]

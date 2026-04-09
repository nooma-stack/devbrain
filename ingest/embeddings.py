"""Embedding helpers using local Ollama."""

from __future__ import annotations

import json
import urllib.request

from config import OLLAMA_URL, EMBED_MODEL

# mxbai-embed-large has a 512 token context window.
# Truncate input to stay within limits (~4 chars per token).
MAX_EMBED_CHARS = 512 * 4  # 2048 chars ≈ 512 tokens


def _truncate(text: str) -> str:
    """Truncate text to fit within the embedding model's context window."""
    if len(text) <= MAX_EMBED_CHARS:
        return text
    return text[:MAX_EMBED_CHARS]


def embed(text: str) -> list[float]:
    data = json.dumps({"model": EMBED_MODEL, "input": _truncate(text)}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result["embeddings"][0]


def embed_batch(texts: list[str]) -> list[list[float]]:
    truncated = [_truncate(t) for t in texts]
    data = json.dumps({"model": EMBED_MODEL, "input": truncated}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result["embeddings"]

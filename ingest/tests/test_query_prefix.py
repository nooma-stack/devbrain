"""Query-vs-document embedding prefix tests (no DB, no network).

Run from the ingest/ directory: ``cd ingest && python -m pytest tests/``
"""

from __future__ import annotations

import json

import pytest

import config
import embeddings


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture
def sent(monkeypatch):
    """Capture the `input` field of every Ollama /api/embed request."""
    captured: list = []

    def fake_urlopen(req, *args, **kwargs):
        body = json.loads(req.data)
        captured.append(body["input"])
        n = len(body["input"]) if isinstance(body["input"], list) else 1
        return _FakeResponse({"embeddings": [[0.1, 0.2]] * n})

    monkeypatch.setattr(embeddings.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(embeddings, "EMBED_QUERY_PREFIX", "query: ")
    return captured


def test_queries_are_prefixed(sent):
    embeddings.embed_query("where is the hnsw index built")
    assert sent == ["query: where is the hnsw index built"]


def test_explicit_empty_prefix_embeds_raw_query(sent):
    embeddings.embed_query("raw", prefix="")
    assert sent == ["raw"]


def test_documents_are_not_prefixed(sent):
    embeddings.embed("Decision title\nbody")
    embeddings.embed_batch(["a", "b"])
    assert sent == ["Decision title\nbody", ["a", "b"]]


def test_default_prefix_is_model_specific():
    assert config.default_query_prefix("snowflake-arctic-embed2") == "query: "
    assert config.default_query_prefix("snowflake-arctic-embed:335m") == "query: "
    assert config.default_query_prefix("nomic-embed-text") == ""


def test_resolve_precedence_env_over_yaml_over_default(monkeypatch):
    cfg = {"embedding": {"model": "snowflake-arctic-embed2"}}
    monkeypatch.delenv("DEVBRAIN_QUERY_PREFIX", raising=False)
    assert config.resolve_query_prefix(cfg) == "query: "
    cfg["embedding"]["query_prefix"] = "q: "
    assert config.resolve_query_prefix(cfg) == "q: "
    monkeypatch.setenv("DEVBRAIN_QUERY_PREFIX", "")
    assert config.resolve_query_prefix(cfg) == ""  # empty env disables

"""Reranker service tests with a fake scorer (no torch, no model download).

Run from the repo root: python -m pytest rerank/tests -q
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402


def fake_scorer(query: str, documents: list[str]) -> list[float]:
    # Relevance = shared words with the query (deterministic, order-preserving).
    q = set(query.lower().split())
    return [float(len(q & set(d.lower().split()))) for d in documents]


@pytest.fixture()
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(fake_scorer, "fake/model", "cpu"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def post(url: str, payload: object) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_health(base_url):
    with urllib.request.urlopen(base_url + "/health", timeout=5) as resp:
        assert json.loads(resp.read()) == {"status": "ok", "model": "fake/model", "device": "cpu"}


def test_rerank_scores_in_document_order(base_url):
    code, body = post(base_url + "/rerank", {"query": "hnsw index recall", "documents": ["cats", "hnsw recall", "index"]})
    assert code == 200
    assert body["scores"] == [0.0, 2.0, 1.0]
    assert body["model"] == "fake/model"


def test_rerank_rejects_bad_requests(base_url):
    assert post(base_url + "/rerank", {"query": "", "documents": []})[0] == 400
    assert post(base_url + "/rerank", {"query": "q", "documents": "nope"})[0] == 400
    assert post(base_url + "/rerank", {"query": "q", "documents": ["x"] * (server.MAX_DOCUMENTS + 1)})[0] == 400
    assert post(base_url + "/nope", {"query": "q", "documents": []})[0] == 404


def test_parse_request_truncates_to_limits():
    q, docs = server.parse_request(json.dumps({"query": "q" * 5000, "documents": ["d" * 9000]}).encode())
    assert len(q) == server.MAX_QUERY_CHARS
    assert len(docs[0]) == server.MAX_DOC_CHARS


def test_scorer_failure_returns_500_without_leaking_text():
    def broken(query, documents):
        raise RuntimeError("secret memory text")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(broken, "m", "cpu"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, body = post(f"http://127.0.0.1:{httpd.server_address[1]}/rerank", {"query": "q", "documents": ["d"]})
        assert code == 500
        assert "secret" not in json.dumps(body)
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.2"])
def test_loopback_hosts_allowed(host):
    server.require_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
def test_non_loopback_hosts_refused(host):
    with pytest.raises(SystemExit):
        server.require_loopback(host)

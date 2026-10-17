"""DevBrain local reranker: a cross-encoder behind a loopback HTTP endpoint.

deep_search (mcp-server/src/rerank.ts) sends the query plus its top
candidates and gets one relevance score per candidate. Everything runs on
this machine: the model weights are downloaded once by install.sh and the
service runs with HF_HUB_OFFLINE=1, so no query or memory text ever leaves
the host (BrightBrain content may contain PHI).

API (JSON):
  GET  /health  -> {"status": "ok", "model": ..., "device": ...}
  POST /rerank  {"query": str, "documents": [str, ...]}
             -> {"scores": [float, ...], "model": ..., "ms": float}
     Scores are the cross-encoder's relevance probabilities (sigmoid of the
     logit, 0..1, higher = more relevant), in the order of `documents`.

Configuration (env):
  DEVBRAIN_RERANK_MODEL       Hugging Face id (default below)
  DEVBRAIN_RERANK_REVISION    optional pinned revision
  DEVBRAIN_RERANK_HOST        bind address; loopback only (default 127.0.0.1)
  DEVBRAIN_RERANK_PORT        default 18600
  DEVBRAIN_RERANK_DEVICE      mps | cpu | cuda (default: mps when available)
  DEVBRAIN_RERANK_MAX_LENGTH  tokens per (query, document) pair (default 512)
  DEVBRAIN_RERANK_BATCH       pairs per forward pass (default 32)
  DEVBRAIN_RERANK_DTYPE       float32 | float16 (default float32)
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Protocol

DEFAULT_MODEL = "Alibaba-NLP/gte-reranker-modernbert-base"
DEFAULT_PORT = 18600
MAX_DOCUMENTS = 200
MAX_QUERY_CHARS = 2000
MAX_DOC_CHARS = 4000
MAX_BODY_BYTES = 4 * 1024 * 1024


class Scorer(Protocol):
    def __call__(self, query: str, documents: list[str]) -> list[float]: ...


class RequestError(ValueError):
    """A bad request; the message is returned to the client with HTTP 400."""


def parse_request(body: bytes) -> tuple[str, list[str]]:
    """Validate a /rerank body and return (query, documents), truncated to limits."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RequestError("body is not JSON") from exc
    if not isinstance(data, dict):
        raise RequestError("body must be a JSON object")
    query = data.get("query")
    docs = data.get("documents")
    if not isinstance(query, str) or not query.strip():
        raise RequestError("query must be a non-empty string")
    if not isinstance(docs, list) or not all(isinstance(d, str) for d in docs):
        raise RequestError("documents must be a list of strings")
    if len(docs) > MAX_DOCUMENTS:
        raise RequestError(f"at most {MAX_DOCUMENTS} documents")
    return query[:MAX_QUERY_CHARS], [d[:MAX_DOC_CHARS] for d in docs]


def load_cross_encoder(
    model: str,
    revision: str | None,
    device: str | None,
    max_length: int,
    batch: int,
    dtype: str = "float32",
) -> tuple[Scorer, str]:
    """Load a sentence-transformers CrossEncoder; returns (scorer, device)."""
    import torch  # noqa: PLC0415 — heavy; tests inject a fake scorer
    from sentence_transformers import CrossEncoder  # noqa: PLC0415

    if not device:
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    kwargs: dict[str, Any] = {"device": device, "max_length": max_length}
    if dtype == "float16":
        kwargs["model_kwargs"] = {"dtype": torch.float16}
    if revision:
        kwargs["revision"] = revision
    ce = CrossEncoder(model, **kwargs)
    lock = threading.Lock()  # one forward pass at a time (MPS is not re-entrant)

    def score(query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        with lock, torch.inference_mode():
            out = ce.predict(
                [(query, d) for d in documents],
                batch_size=batch,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        return [float(x) for x in out]

    return score, device


def make_handler(scorer: Scorer, model: str, device: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "devbrain-rerank/1"

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"status": "ok", "model": model, "device": device})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/rerank":
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(413 if length > MAX_BODY_BYTES else 400, {"error": "bad Content-Length"})
                return
            try:
                query, docs = parse_request(self.rfile.read(length))
            except RequestError as exc:
                self._send(400, {"error": str(exc)})
                return
            t0 = time.monotonic()
            try:
                scores = scorer(query, docs)
            except Exception as exc:  # noqa: BLE001 — report, keep serving
                self._send(500, {"error": f"scoring failed: {type(exc).__name__}"})
                return
            self._send(200, {"scores": scores, "model": model, "ms": round((time.monotonic() - t0) * 1000, 1)})

        def log_message(self, fmt: str, *args: Any) -> None:
            # Never log bodies (memory text may be sensitive); request lines only.
            sys.stderr.write(f"[rerank] {self.address_string()} {fmt % args}\n")

    return Handler


def require_loopback(host: str) -> None:
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = host == "localhost"
    if not ok:
        raise SystemExit(f"refusing to bind {host!r}: the reranker serves loopback only")


def main(load: Callable[..., tuple[Scorer, str]] = load_cross_encoder) -> None:
    model = os.environ.get("DEVBRAIN_RERANK_MODEL", DEFAULT_MODEL)
    host = os.environ.get("DEVBRAIN_RERANK_HOST", "127.0.0.1")
    port = int(os.environ.get("DEVBRAIN_RERANK_PORT", DEFAULT_PORT))
    require_loopback(host)
    scorer, device = load(
        model,
        os.environ.get("DEVBRAIN_RERANK_REVISION") or None,
        os.environ.get("DEVBRAIN_RERANK_DEVICE") or None,
        int(os.environ.get("DEVBRAIN_RERANK_MAX_LENGTH", 512)),
        int(os.environ.get("DEVBRAIN_RERANK_BATCH", 32)),
        os.environ.get("DEVBRAIN_RERANK_DTYPE", "float32"),
    )
    scorer("warm up", ["warm up the model and the device kernels"] * 4)
    httpd = ThreadingHTTPServer((host, port), make_handler(scorer, model, device))
    print(f"[rerank] {model} on {device}, listening on {host}:{port}", file=sys.stderr, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

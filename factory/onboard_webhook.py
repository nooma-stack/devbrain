"""Onboarding webhook receiver.

Lightweight HTTP service that accepts dev onboarding submissions:

  POST /onboard/<token>/pubkey       JSON: {"pubkey": "ssh-ed25519 ..."}
  POST /onboard/<token>/oauth-token  JSON: {"oauth_token": "sk-ant-oat01-..."}
  GET  /onboard/<token>/status       returns invitation state

The dev (or their AI agent) hits these endpoints with the raw invite
token in the URL. Token validation, single-use enforcement, status
machine, and DB writes are all delegated to factory/invitations.py —
this service is a thin HTTP wrapper.

Deployment shape:
  - Runs in a Docker container on the Mac Studio (devbrain-onboard
    service in docker-compose.yml).
  - Binds to 0.0.0.0:8000 inside the container; published to 127.0.0.1:8000
    on the host (loopback only).
  - A reverse SSH tunnel from Mac Studio to the LHT VPS forwards
    127.0.0.1:8000 (Mac Studio) → 127.0.0.1:8000 (VPS).
  - VPS Traefik routes https://devbrain.lighthouse-therapy.com/onboard/*
    to localhost:8000 on the VPS, which surfaces this service.

Why stdlib instead of FastAPI/Flask: 3 endpoints, JSON in/out, no
auth middleware needed (token IS the auth) — a stdlib http.server
implementation is ~200 lines, has no extra Docker image weight, and
no dependency-update treadmill. If we ever need WebSockets or async
I/O we'll revisit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional


logger = logging.getLogger("devbrain.onboard")

# Token pattern is dvbn_inv_<40-char hex>. We accept upper or lower
# case in the URL but normalize before hashing so a typo'd-case token
# doesn't silently miss an otherwise-valid invitation.
_TOKEN_RE = re.compile(r"^dvbn_inv_[A-Fa-f0-9]{40}$")
# Cap accepted body size to a kilobyte to defend against memory-burner
# requests. Real bodies are <1 KB (an SSH ed25519 pubkey is ~80 chars,
# RSA pubkey is ~700, and the OAuth token is ~120).
_MAX_BODY_BYTES = 4096


def _build_db():
    """Construct the FactoryDB instance from environment.

    Imports inside the function so this module can be imported at test
    time without a live DB. Hand-rolls the connection string from
    component env vars rather than reusing factory/config.py because
    config.py reads from devbrain.yaml which isn't mounted into the
    onboard container.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from state_machine import FactoryDB

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        host = os.environ.get("PGHOST", "devbrain-db")
        port = os.environ.get("PGPORT", "5432")
        user = os.environ.get("PGUSER", "devbrain")
        password = os.environ["PGPASSWORD"]  # required
        database = os.environ.get("PGDATABASE", "devbrain")
        db_url = f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return FactoryDB(db_url)


class OnboardHandler(BaseHTTPRequestHandler):
    """One handler instance per request (the default for ThreadingHTTPServer).

    All routing logic is in `_dispatch`. The `do_POST` / `do_GET` methods
    just funnel into it.
    """

    server_version = "DevBrainOnboard/1.0"

    # Quiet down the per-request log line that BaseHTTPRequestHandler
    # writes to stderr — we log at the dispatch layer with structured
    # info instead.
    def log_message(self, fmt: str, *args) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ─── Routing ──────────────────────────────────────────────────────

    def do_POST(self) -> None:
        self._dispatch(method="POST")

    def do_GET(self) -> None:
        self._dispatch(method="GET")

    def _dispatch(self, method: str) -> None:
        try:
            self._dispatch_inner(method)
        except Exception:
            logger.exception("Unhandled error processing %s %s", method, self.path)
            self._json(500, {"status": "error", "error": "internal"})

    def _dispatch_inner(self, method: str) -> None:
        # Path patterns. Strip query string defensively.
        path = self.path.split("?", 1)[0].rstrip("/")
        # Pattern: /onboard/<token>/<action>
        m = re.match(r"^/onboard/([^/]+)/([a-z-]+)$", path)
        # Health check: lets the load balancer / launchd verify liveness.
        if path == "/healthz" and method == "GET":
            self._json(200, {"status": "ok"})
            return
        if not m:
            self._json(404, {"status": "error", "error": "not_found"})
            return

        raw_token, action = m.group(1), m.group(2)

        # Validate token shape early so noisy requests don't burn DB lookups.
        if not _TOKEN_RE.match(raw_token):
            self._json(404, {"status": "error", "error": "invalid_token_format"})
            return

        if action == "pubkey" and method == "POST":
            self._handle_pubkey(raw_token)
        elif action == "oauth-token" and method == "POST":
            self._handle_oauth_token(raw_token)
        elif action == "status" and method == "GET":
            self._handle_status(raw_token)
        else:
            self._json(405, {"status": "error", "error": "method_not_allowed"})

    # ─── Handlers ─────────────────────────────────────────────────────

    def _handle_pubkey(self, raw_token: str) -> None:
        body = self._read_json_body()
        if body is None:
            return
        pubkey = body.get("pubkey")
        if not isinstance(pubkey, str) or not pubkey.strip():
            self._json(400, {"status": "error", "error": "missing_pubkey"})
            return

        from invitations import submit_pubkey
        db = _build_db()
        inv = submit_pubkey(db, raw_token, pubkey)
        if inv is None:
            self._json(410, {"status": "error", "error": "invalid_or_expired_or_replayed"})
            return
        self._json(200, {
            "status": "ok",
            "invitation_status": inv.status,
            "pubkey_received": True,
            "oauth_token_received": inv.oauth_token is not None,
        })
        logger.info("pubkey received: dev=%s status=%s", inv.dev_id, inv.status)

    def _handle_oauth_token(self, raw_token: str) -> None:
        body = self._read_json_body()
        if body is None:
            return
        oauth_token = body.get("oauth_token")
        if not isinstance(oauth_token, str) or not oauth_token.strip():
            self._json(400, {"status": "error", "error": "missing_oauth_token"})
            return

        from invitations import submit_oauth_token
        db = _build_db()
        inv = submit_oauth_token(db, raw_token, oauth_token)
        if inv is None:
            self._json(410, {"status": "error", "error": "invalid_or_expired_or_replayed"})
            return
        # NEVER log the token. Log only the metadata.
        self._json(200, {
            "status": "ok",
            "invitation_status": inv.status,
            "pubkey_received": inv.pubkey is not None,
            "oauth_token_received": True,
        })
        logger.info("oauth_token received: dev=%s status=%s", inv.dev_id, inv.status)

    def _handle_status(self, raw_token: str) -> None:
        from invitations import get_invitation_by_token
        db = _build_db()
        inv = get_invitation_by_token(db, raw_token)
        if inv is None:
            self._json(404, {"status": "error", "error": "not_found"})
            return
        # Don't expose any of the sensitive fields — just the state.
        self._json(200, {
            "status": "ok",
            "invitation_status": inv.status,
            "pubkey_received": inv.pubkey is not None,
            "oauth_token_received": inv.oauth_token is not None,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            "activated_at": inv.activated_at.isoformat() if inv.activated_at else None,
        })

    # ─── Helpers ──────────────────────────────────────────────────────

    def _read_json_body(self) -> Optional[dict]:
        """Parse the request body as JSON. Sends an error response on failure."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"status": "error", "error": "invalid_content_length"})
            return None
        if length <= 0:
            self._json(400, {"status": "error", "error": "missing_body"})
            return None
        if length > _MAX_BODY_BYTES:
            self._json(413, {"status": "error", "error": "payload_too_large"})
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"status": "error", "error": "invalid_json"})
            return None

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Defensive headers — this service shouldn't be cached or framed.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the webhook server forever (blocks). Used as the launchd cmd.

    Implementation note: serve_forever() is invoked in a worker thread,
    not directly in the main thread. On Python 3.14 + macOS, calling
    `ThreadingHTTPServer.serve_forever()` directly in the main thread
    of a backgrounded or launchd-managed process drops the listening
    socket into CLOSED state immediately — the bind succeeds but the
    server never accepts connections. Running it in a worker thread
    while the main thread waits on join() works around the issue and
    has been verified under both shell-backgrounded and launchd-managed
    contexts. See onboarding-webhook-deploy.md for the diagnostic trail.
    """
    import threading

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting devbrain-onboard on %s:%s", host, port)
    # Plain HTTPServer (single-threaded). Adequate for the onboarding
    # use case — submissions are infrequent (one new dev per week tops)
    # and per-request work is a single Postgres roundtrip.
    server = HTTPServer((host, port), OnboardHandler)

    serve_thread = threading.Thread(
        target=server.serve_forever,
        name="onboard-webhook-serve",
        daemon=False,
    )
    serve_thread.start()
    try:
        serve_thread.join()
    except KeyboardInterrupt:
        logger.info("Received SIGINT, shutting down")
        server.shutdown()
        serve_thread.join(timeout=5)


if __name__ == "__main__":
    serve(
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("BIND_PORT", "8000")),
    )

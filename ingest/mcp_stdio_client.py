"""Minimal MCP stdio client — call one devbrain tool from a worker script.

The backfill worker must route ``end_session`` through the REAL MCP
server so every side effect happens exactly as it does for live agents:
summary chunk + memory rows + embedding, the ``end_session_log`` row
(with ``cli`` provenance), curator enrichment, and the fanout trigger.
Re-implementing that in the worker would drift; a 100-line stdio client
cannot.

MCP stdio transport is newline-delimited JSON-RPC 2.0. We do the minimum
handshake (initialize → notifications/initialized) and one tools/call.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_DEFAULT_SERVER = str(
    Path(__file__).resolve().parent.parent / "mcp-server" / "dist" / "index.js"
)


class McpError(RuntimeError):
    pass


def call_tool(tool: str, arguments: dict, *, server_js: str | None = None,
              env_extra: dict | None = None, timeout_s: int = 300) -> str:
    """Spawn the MCP server, call one tool, return its text response."""
    env = dict(os.environ)
    env.setdefault("DEVBRAIN_MCP_SKIP_DB_PROBE", "")
    if env_extra:
        env.update(env_extra)

    proc = subprocess.Popen(
        ["node", server_js or _DEFAULT_SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env,
    )

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(expect_id: int) -> dict:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                err = (proc.stderr.read() or "")[-800:] if proc.stderr else ""
                raise McpError(f"server closed stdout; stderr tail: {err}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # server log noise on stdout
            if msg.get("id") == expect_id:
                return msg

    try:
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "devbrain-closure-worker", "version": "1.0"},
            },
        })
        recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
        resp = recv(2)
        if "error" in resp:
            raise McpError(f"tools/call error: {resp['error']}")
        content = (resp.get("result") or {}).get("content") or []
        texts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return "\n".join(t for t in texts if t)
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
            proc.wait(timeout=timeout_s)
        except Exception:
            proc.kill()

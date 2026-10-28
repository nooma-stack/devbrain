#!/usr/bin/env python3
"""DevBrain prompt recall: a Claude Code UserPromptSubmit hook.

Claude Code hands this hook the prompt on stdin as JSON (prompt, cwd,
session_id). It asks DevBrain's `recall` tool, through the HTTPS gateway, for
the two or three memories that match the prompt best and hands them back as
additionalContext. When DevBrain has nothing strong to say, the tool returns
nothing and so does this hook.

It never blocks or slows a prompt for long: one HTTPS request, a hard 1.5 s
total deadline, and on any failure at all (no project, no token, network,
401, timeout, bad response) it prints nothing and exits 0.

Python 3 standard library only.

Configuration (all optional except the token and a project):
  ~/.config/devbrain/prompt-recall.token   gateway bearer token (dbg_...), mode 600
  ~/.config/devbrain/prompt-recall.json    {"<path prefix>": "<project slug>", ...}
                                           longest matching prefix wins
  DEVBRAIN_PROJECT            project slug; wins over the map file
  DEVBRAIN_RECALL_URL         gateway MCP endpoint (default https://devbrain.trifekta.ai/mcp)
  DEVBRAIN_RECALL_MAX_CHARS   context budget (default 600)
  DEVBRAIN_RECALL=off         turn the hook off without editing settings.json
  DEVBRAIN_RECALL_DEBUG=1     say on stderr why nothing was added
"""

from __future__ import annotations

import ipaddress
import json
import os
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://devbrain.trifekta.ai/mcp"
CONFIG_DIR = os.path.join("~", ".config", "devbrain")
MAP_FILE = os.path.join(CONFIG_DIR, "prompt-recall.json")
TOKEN_FILE = os.path.join(CONFIG_DIR, "prompt-recall.token")

TOTAL_TIMEOUT_S = 1.5
MIN_PROMPT_CHARS = 15
DEFAULT_MAX_CHARS = 600
# The server uses the first 1500 characters; send a little more, not a pasted log.
MAX_PROMPT_SEND = 2000
# Prompts that are hook output or another session's relay, not a person typing.
RELAY_PREFIXES = ("<", "Another Claude session")
# What a recall answer starts with; anything else is not injected.
ANSWER_PREFIX = "devbrain:"
USER_AGENT = "devbrain-prompt-recall/1"


class Skip(Exception):
    """Nothing to add; the message is for DEVBRAIN_RECALL_DEBUG only."""


def debug(env: dict, msg: str) -> None:
    if env.get("DEVBRAIN_RECALL_DEBUG") == "1":
        print(f"prompt-recall: {msg}", file=sys.stderr)


# ─── skip rules ─────────────────────────────────────────────────────────────


def skip_reason(prompt: object) -> str | None:
    """Why this prompt gets no recall, or None when it should be looked up."""
    if not isinstance(prompt, str):
        return "no prompt"
    text = prompt.strip()
    if len(text) < MIN_PROMPT_CHARS:
        return "prompt too short"
    if text.startswith("/"):
        return "slash command"
    if text.startswith(RELAY_PREFIXES):
        return "hook or session relay"
    return None


# ─── cwd → project ──────────────────────────────────────────────────────────


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip(os.sep) + os.sep)


def load_map(path: str) -> dict[str, str]:
    """The prefix → slug map, or {} when absent or unreadable."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        _norm(k): v.strip()
        for k, v in data.items()
        if isinstance(k, str)
        and k
        and not k.startswith("_")
        and isinstance(v, str)
        and v.strip()
    }


def project_for(cwd: str, env: dict, map_path: str) -> str | None:
    """DEVBRAIN_PROJECT, else the longest map prefix containing cwd, else None."""
    slug = (env.get("DEVBRAIN_PROJECT") or "").strip()
    if slug:
        return slug
    mapping = load_map(map_path)
    if not mapping or not cwd:
        return None
    candidates = {_norm(cwd)}
    try:
        candidates.add(os.path.realpath(cwd))
    except OSError:
        pass
    best: tuple[int, str] | None = None
    for prefix, slug in mapping.items():
        if any(_under(c, prefix) for c in candidates):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), slug)
    return best[1] if best else None


# ─── token and URL ──────────────────────────────────────────────────────────


def read_token(path: str) -> str:
    """The bearer token. Refused when group/other can read the file (like ssh)."""
    full = os.path.expanduser(path)
    try:
        st = os.stat(full)
    except OSError:
        raise Skip(f"no token file at {full}")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise Skip(f"{full} must be mode 600 (chmod 600 {full})")
    try:
        with open(full, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        raise Skip("token file unreadable")
    if not token or any(c.isspace() for c in token):
        raise Skip("token file is empty or malformed")
    return token


def check_url(url: str) -> str:
    """HTTPS only, except plain HTTP to a loopback address (tests, a local gateway)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname:
        host = parts.hostname
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            return url
    raise Skip("DEVBRAIN_RECALL_URL must be https:// (or http:// to localhost)")


# ─── the call ───────────────────────────────────────────────────────────────


def build_request(
    url: str, token: str, prompt: str, project: str, max_chars: int
) -> urllib.request.Request:
    # The gateway is stateless MCP (Streamable HTTP, JSON responses, no
    # session id): a lone tools/call needs no initialize, so this is the only
    # round trip.
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "recall",
            "arguments": {
                "prompt": prompt[:MAX_PROMPT_SEND],
                "project": project,
                "max_chars": max_chars,
            },
        },
    }
    return urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
    )


def parse_response(raw: bytes, content_type: str) -> str:
    """The recall text from a JSON-RPC response (JSON or a one-event SSE stream)."""
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in (content_type or ""):
        data = [
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        ]
        text = data[-1] if data else ""
    try:
        msg = json.loads(text)
    except ValueError:
        raise Skip("response is not JSON")
    if not isinstance(msg, dict) or "error" in msg:
        raise Skip(
            f"gateway error: {msg.get('error') if isinstance(msg, dict) else msg}"
        )
    result = msg.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise Skip("recall returned an error")
    parts = [
        c.get("text", "")
        for c in result.get("content") or []
        if isinstance(c, dict)
        and c.get("type") == "text"
        and isinstance(c.get("text"), str)
    ]
    out = "\n".join(p for p in parts if p).strip()
    if not out:
        raise Skip("nothing strong enough to recall")
    if not out.startswith(ANSWER_PREFIX):
        raise Skip("unexpected answer shape")
    return out


def call_recall(req: urllib.request.Request, deadline: float) -> str:
    """POST with a hard wall-clock deadline (urllib's timeout is per socket op)."""
    box: dict = {}

    def run() -> None:
        try:
            left = max(0.05, deadline - time.monotonic())
            with urllib.request.urlopen(req, timeout=left) as resp:
                box["body"] = resp.read(256 * 1024)
                box["ctype"] = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            box["err"] = f"HTTP {e.code}"
        except BaseException as e:  # noqa: BLE001 - fail open on anything
            box["err"] = f"{type(e).__name__}: {e}"

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        raise Skip("timed out")
    if "err" in box:
        raise Skip(box["err"])
    return parse_response(box.get("body", b""), box.get("ctype", ""))


# ─── entry point ────────────────────────────────────────────────────────────


def _int_env(env: dict, name: str, default: int) -> int:
    try:
        return int(env.get(name, "")) if env.get(name) else default
    except ValueError:
        return default


def recall_context(hook: dict, env: dict, started: float) -> str:
    """The additionalContext text; raises Skip when there is none."""
    if (env.get("DEVBRAIN_RECALL") or "").strip().lower() in (
        "off",
        "0",
        "false",
        "no",
    ):
        raise Skip("DEVBRAIN_RECALL is off")
    prompt = hook.get("prompt")
    reason = skip_reason(prompt)
    if reason:
        raise Skip(reason)
    cwd = hook.get("cwd") if isinstance(hook.get("cwd"), str) else os.getcwd()
    project = project_for(cwd, env, env.get("DEVBRAIN_RECALL_MAP") or MAP_FILE)
    if not project:
        raise Skip(f"no project for {cwd}")
    url = check_url(env.get("DEVBRAIN_RECALL_URL") or DEFAULT_URL)
    token = read_token(env.get("DEVBRAIN_RECALL_TOKEN_FILE") or TOKEN_FILE)
    # The recall tool accepts 300-4000.
    max_chars = min(
        4000, max(300, _int_env(env, "DEVBRAIN_RECALL_MAX_CHARS", DEFAULT_MAX_CHARS))
    )
    deadline = started + TOTAL_TIMEOUT_S
    return call_recall(
        build_request(url, token, prompt.strip(), project, max_chars), deadline
    )


def main(stdin=None, stdout=None, env=None) -> int:
    started = time.monotonic()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    env = dict(os.environ if env is None else env)
    try:
        hook = json.load(stdin)
        if not isinstance(hook, dict):
            raise Skip("hook input is not an object")
        text = recall_context(hook, env, started)
    except Skip as s:
        debug(env, f"{s} ({round((time.monotonic() - started) * 1000)} ms)")
        return 0
    except BaseException as e:  # noqa: BLE001 - a prompt hook must never fail the prompt
        debug(env, f"{type(e).__name__}: {e}")
        return 0
    stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": text,
                }
            }
        )
    )
    stdout.flush()
    debug(
        env,
        f"added {len(text)} chars ({round((time.monotonic() - started) * 1000)} ms)",
    )
    return 0


if __name__ == "__main__":
    try:
        main()
    finally:
        # os._exit: never wait on a still-running request thread.
        try:
            sys.stdout.flush()
        except BaseException:  # noqa: BLE001
            pass
        os._exit(0)

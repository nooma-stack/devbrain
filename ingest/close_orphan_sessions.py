#!/usr/bin/env python3 -u
"""Find sessions that never called end_session; close them out properly.

Detection is MECHANICAL and needs no sentinel convention: a properly
closed session carries an ``mcp__*__end_session`` tool_use in its own
transcript (see session_closure.py). The same scan extracts the
devbrain ``conversation_uuid`` and the last breadcrumb position, so the
backfill summarizes only the un-checkpointed TAIL and links its
end_session onto the existing chain.

Backfill quality ladder (pick with --backend):
    resume     — cold-resume the ORIGINAL session with `claude --resume`
                 and let the model that lived it write its own
                 end_session. Highest quality; needs the claude CLI and
                 the session belonging to this machine/user.
    openai     — OpenAI API (gpt-5.6-luna default; auto-escalates to
                 terra when luna's output fails validation). 1.05M-token
                 window on both, so whole transcripts fit. This is the
                 BAA-covered rail for LHT content.
    codex      — big-context model via codex CLI on the ChatGPT sub
                 (gpt-5.6-luna/terra verified reachable). Prompt goes via
                 stdin, so million-char inputs are fine. SUBSCRIPTION
                 BUDGET: use for bounded backfills, not steady state.
    openrouter — cheap open model, metered pennies. Set both `zdr` and
                 data_collection=deny. Steady-state default for non-PHI.
    ollama     — local qwen fold over breadcrumbs + head/tail. $0,
                 always available, the floor.

PHI guard: remote backends (openai/codex/openrouter) refuse to run
unless DEVBRAIN_CLOSURE_REMOTE_OK=1 is set. Setting it asserts the
content/backend pairing is compliant — e.g. the OpenAI API under the
org's BAA for LHT content, or personal non-PHI content anywhere.

The end_session write goes through the REAL MCP server (stdio client),
so logging, enrichment, embedding, and fanout all happen exactly as for
a live agent. Provenance: DEVBRAIN_MCP_CLI=closure-backfill lands in
end_session_log.cli.

Usage:
    close_orphan_sessions.py --report                       # scan only
    close_orphan_sessions.py --backend ollama --limit 5
    close_orphan_sessions.py --backend resume --limit 3
    close_orphan_sessions.py --backend codex --model gpt-5.6-luna --limit 10
    close_orphan_sessions.py --backend openai --limit 600   # luna, terra escalation
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import psycopg2

from config import DATABASE_URL, OLLAMA_URL
from session_closure import (extract_text, extract_usf_text, parse_transcript,
                             parse_usf)

PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decisions_made": {"type": "array", "items": {"type": "string"}},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "issues_found": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "decisions_made", "files_changed",
                 "issues_found", "next_steps"],
    "additionalProperties": False,
}

PROMPT_HEADER = """You are closing out an AI work session that ended without a proper \
end_session summary. Using the breadcrumb checkpoints (already-summarized earlier \
segments) and the transcript tail below, produce the end_session report. Be specific \
about file names and technical detail; be honest about incomplete work — do NOT \
describe unfinished steps as done. Respond ONLY with JSON matching this schema:
{schema}

=== BREADCRUMB CHECKPOINTS (earlier segments, highest fidelity) ===
{breadcrumbs}

=== TRANSCRIPT (from last checkpoint onward) ===
{tail}
"""

# Input budgets per backend (chars). ~3.5M chars ≈ 1M tokens: the
# gpt-5.6 window, so the head+tail cap almost never fires on "openai".
BUDGETS = {"ollama": 90_000, "codex": 2_000_000, "openrouter": 1_200_000,
           "openai": 3_500_000}


def _salvage_json(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    last, skip = None, -1
    for i, ch in enumerate(text):
        if ch != "{" or i < skip:
            continue
        try:
            obj, n = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        skip = i + n
        if isinstance(obj, dict):
            last = obj
    return last


def _validate(payload: dict) -> dict | None:
    if not isinstance(payload, dict) or not payload.get("summary"):
        return None
    out = {"summary": str(payload["summary"])}
    for key in ("decisions_made", "files_changed", "issues_found", "next_steps"):
        val = payload.get(key) or []
        out[key] = [str(v) for v in val if str(v).strip()][:30]
    return out


# ── backends ────────────────────────────────────────────────────────────────

def run_ollama(prompt: str, model: str) -> dict | None:
    body = {"model": model, "prompt": prompt, "stream": False, "think": False,
            "format": "json",
            "options": {"temperature": 0.3, "num_predict": 2000,
                        "num_ctx": min(131072, len(prompt) // 3 + 3000)}}
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        resp = json.loads(r.read()).get("response", "")
    return _salvage_json(resp)


def run_codex(prompt: str, model: str) -> dict | None:
    with tempfile.TemporaryDirectory(prefix="closure-codex-") as wd:
        schema_path = os.path.join(wd, "schema.json")
        out_path = os.path.join(wd, "out.json")
        with open(schema_path, "w") as fh:
            json.dump(PAYLOAD_SCHEMA, fh)
        proc = subprocess.run(
            ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
             "--ephemeral", "-C", wd, "--output-schema", schema_path,
             "-o", out_path, "-m", model, "-"],
            input=prompt, capture_output=True, text=True, timeout=1800)
        if os.path.exists(out_path):
            with open(out_path) as fh:
                return json.load(fh)
        return _salvage_json((proc.stdout or "") + (proc.stderr or ""))


def run_openai(prompt: str, model: str) -> dict | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY not set")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "end_session_payload", "strict": True,
            "schema": PAYLOAD_SCHEMA}},
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        data = json.loads(r.read())
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _salvage_json(text)


def run_openrouter(prompt: str, model: str) -> dict | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY not set")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # Privacy floor: ZDR endpoints only, and never train on the data.
        "provider": {"zdr": True, "data_collection": "deny"},
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        data = json.loads(r.read())
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _salvage_json(text)


def run_resume(session_uuid: str, limit_note: str) -> bool:
    """Cold-resume the original session; its own model writes end_session."""
    prompt = (
        "You are being resumed for one purpose only: this session was never "
        "closed out. Call the devbrain/brightbrain end_session tool NOW with "
        "an honest structured summary of this session (use your saved "
        "conversation_uuid as session_id if you have one). Do not do any "
        "other work. " + limit_note)
    proc = subprocess.run(
        ["claude", "--resume", session_uuid, "-p", prompt,
         "--dangerously-skip-permissions"],
        capture_output=True, text=True, timeout=900)
    return proc.returncode == 0


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="scan + report only")
    ap.add_argument("--backend",
                    choices=["resume", "openai", "codex", "openrouter", "ollama"],
                    default="ollama")
    ap.add_argument("--model", default=None,
                    help="model id (default: qwen3.8:27b / gpt-5.6-luna / "
                         "per-backend sensible default)")
    ap.add_argument("--limit", type=int, default=5, help="max sessions to close")
    ap.add_argument("--settle-hours", type=int, default=6,
                    help="only sessions idle at least this long")
    ap.add_argument("--project", default=None, help="restrict to one project slug")
    args = ap.parse_args()

    if args.backend in ("openai", "codex", "openrouter") and \
            os.environ.get("DEVBRAIN_CLOSURE_REMOTE_OK") != "1":
        raise SystemExit(
            "Remote backend refused: transcripts may contain sensitive/PHI "
            "content. Set DEVBRAIN_CLOSURE_REMOTE_OK=1 only on machines whose "
            "transcripts are cleared for remote processing.")

    model = args.model or {"ollama": "qwen3.8:27b", "codex": "gpt-5.6-luna",
                           "openai": "gpt-5.6-luna",
                           "openrouter": "qwen/qwen3-235b-a22b",
                           "resume": ""}[args.backend]

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    where = ["rs.source_app = 'claude_code'",
             "rs.created_at < now() - make_interval(hours => %s)"]
    params: list[object] = [args.settle_hours]
    if args.project:
        where.append("p.slug = %s")
        params.append(args.project)
    cur.execute(
        f"""SELECT rs.id, rs.session_id, rs.source_path, p.slug
            FROM devbrain.raw_sessions rs
            JOIN devbrain.projects p ON rs.project_id = p.id
            WHERE {' AND '.join(where)}
            ORDER BY rs.created_at DESC""", params)
    rows = cur.fetchall()

    # A backfilled session's transcript never gains the end_session
    # tool_use, so transcript-scanning alone would re-backfill it forever.
    # end_session_log is the second half of the truth: anything already
    # logged (by chain uuid OR by the backfill-<transcript-uuid> id) is
    # closed.
    cur.execute("SELECT session_id FROM devbrain.end_session_log")
    logged_ids = {r[0] for r in cur.fetchall()}

    stats = {"scanned": 0, "closed": 0, "unclosed": 0, "from_db_copy": 0,
             "already_logged": 0, "backfilled": 0, "failed": 0}
    todo: list[tuple[str, str, str | None, str, object]] = []
    for row_id, session_uuid, source_path, slug in rows:
        stats["scanned"] += 1
        # Older ingests carry NULL session_id (843 rows on the LHT DB);
        # raw_sessions.id is the always-present identity. Without this
        # fallback every NULL row would share the dedup id
        # "backfill-None" and the first closure would mark them ALL as
        # already-backfilled.
        ident = session_uuid or f"row-{row_id}"
        if source_path and Path(source_path).exists():
            facts = parse_transcript(source_path)
        else:
            # Transcript pruned from disk (Claude Code retention) — fall
            # back to the USF copy in raw_sessions.raw_content, which
            # preserves tool calls and therefore has full closure parity.
            # Fetched lazily per row: raw_content is large.
            cur.execute("SELECT raw_content FROM devbrain.raw_sessions "
                        "WHERE id = %s", (row_id,))
            raw = (cur.fetchone() or [None])[0]
            if not raw:
                continue
            stats["from_db_copy"] += 1
            # 998 pre-USF rows on the LHT DB store raw_content as PLAIN
            # TEXT, not USF JSON. parse_usf returns empty facts for those
            # — which is fine (closure falls back to the end_session_log
            # guard) — but the TEXT ITSELF is the extracted transcript
            # and must be fed to the model as-is, not run through the
            # JSON extractor (which returns "" and produced 741 paid
            # 'the transcript was empty' summaries on 2026-08-30).
            facts = parse_usf(raw)
            source_path = None  # signals the DB-copy extraction path below
        if facts.closed:
            stats["closed"] += 1
            continue
        if (f"backfill-{ident}" in logged_ids
                or (facts.conversation_uuid
                    and facts.conversation_uuid in logged_ids)):
            stats["already_logged"] += 1
            continue
        stats["unclosed"] += 1
        if len(todo) < args.limit:
            todo.append((ident, str(row_id), source_path, slug, facts))
            continue

    print(f"scan: {stats['scanned']} sessions — {stats['closed']} closed, "
          f"{stats['already_logged']} already-backfilled, "
          f"{stats['unclosed']} unclosed "
          f"(of which {stats['from_db_copy']} via DB copy, file gone)",
          flush=True)
    if args.report:
        return 0

    from mcp_stdio_client import call_tool  # noqa: PLC0415

    for ident, row_id, source_path, slug, facts in todo:
        t0 = time.time()
        try:
            if args.backend == "resume":
                if not source_path:
                    stats["failed"] += 1
                    print(f"  {ident[:16]}: resume needs the transcript "
                          "file; use a model backend for DB-copy rows",
                          flush=True)
                    continue
                ok = run_resume(ident, "")
                print(f"  resume {ident[:16]}: "
                      f"{'ok' if ok else 'FAILED'} ({time.time()-t0:.0f}s)",
                      flush=True)
                stats["backfilled" if ok else "failed"] += 1
                continue

            crumbs = "\n\n".join(
                f"[checkpoint] {c.get('title', '')}\n{c.get('content', '')}"
                for c in facts.breadcrumbs) or "(none recorded)"
            if source_path:
                tail = extract_text(source_path,
                                    from_line=facts.last_breadcrumb_line or 0,
                                    head_tail_chars=BUDGETS[args.backend])
            else:
                cur.execute("SELECT raw_content FROM devbrain.raw_sessions "
                            "WHERE id = %s", (row_id,))
                raw = (cur.fetchone() or [""])[0] or ""
                if raw.lstrip().startswith("{"):
                    tail = extract_usf_text(
                        raw, from_index=facts.last_breadcrumb_line or 0,
                        head_tail_chars=BUDGETS[args.backend])
                else:
                    # Pre-USF era: raw_content IS the extracted text.
                    text = raw
                    budget = BUDGETS[args.backend]
                    if len(text) > budget:
                        half = budget // 2
                        text = (text[:half]
                                + "\n\n[... middle elided for length ...]\n\n"
                                + text[-half:])
                    tail = text
            if not tail.strip():
                stats["failed"] += 1
                print(f"  {ident[:16]}: no source text available — skipping "
                      "instead of paying for an empty-transcript summary",
                      flush=True)
                continue
            prompt = PROMPT_HEADER.format(
                schema=json.dumps(PAYLOAD_SCHEMA), breadcrumbs=crumbs, tail=tail)
            runner = {"ollama": run_ollama, "codex": run_codex,
                      "openai": run_openai,
                      "openrouter": run_openrouter}[args.backend]
            payload = _validate(runner(prompt, model) or {})
            if not payload and args.backend == "openai" and model == "gpt-5.6-luna":
                # Quality ladder: luna is the cheap default; a validation
                # failure escalates that ONE session to terra before giving up.
                payload = _validate(run_openai(prompt, "gpt-5.6-terra") or {})
            if not payload:
                stats["failed"] += 1
                print(f"  {ident[:16]}: model produced no valid payload",
                      flush=True)
                continue
            call_tool("end_session", {
                "project": slug,
                "session_id": facts.conversation_uuid or f"backfill-{ident}",
                **payload,
            }, env_extra={"DEVBRAIN_MCP_CLI": "closure-backfill",
                          "DEVBRAIN_PROJECT": slug})
            stats["backfilled"] += 1
            print(f"  {ident[:16]} [{slug}] backfilled via {args.backend} "
                  f"({time.time()-t0:.0f}s, tail from line "
                  f"{facts.last_breadcrumb_line or 0})", flush=True)
        except Exception as exc:  # keep the batch going
            stats["failed"] += 1
            print(f"  {ident[:16]}: ERROR {str(exc)[:200]}", flush=True)

    print(f"done: {stats['backfilled']} backfilled, {stats['failed']} failed",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

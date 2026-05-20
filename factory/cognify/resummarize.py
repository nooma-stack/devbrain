"""cognify_resummarize — upgrade Ollama summaries to Sonnet for orphan sessions.

The ingest watcher writes an initial session_summary at ingest time using
local Ollama (qwen2.5:7b — free, lower quality). Most sessions then get
an agent-curated summary via end_session, which supersedes the Ollama
version in deep_search ranking.

For sessions where end_session was NEVER called (long-running automation,
abandoned conversations, mid-stream errors), the Ollama summary stays
the canonical row — and search results degrade accordingly.

This pass closes that gap: it scans for raw_sessions whose summary_source
is 'ollama' (or NULL — pre-migration-041) AND has no end_session_log
entry AND the conversation is settled. For each, it re-summarizes with
Sonnet 4.6, replaces the summary + session_summary chunks, and marks
summary_source='sonnet' so the pass is idempotent.

Cost: ~$0.01–0.02 per session (Sonnet 4.6 with ~9K avg input tokens).
The all-time backfill on a ~3K-session DB lands around $30–60.

Schedule: 60-min launchd cadence; manual trigger via
`bin/devbrain cognify-resummarize`.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sonnet is the right tradeoff for summarization — strong reasoning, much
# cheaper than Opus. Caller can override via --model.
DEFAULT_MODEL = "claude-sonnet-4-6"

# How long after the last raw_sessions write before we consider the
# session "settled" and safe to upgrade. Avoids running on a still-
# active session where the agent hasn't had a chance to call end_session.
SETTLED_AFTER_MINUTES = 30

# Per-call output cap. Sonnet summaries top out around 800 chars; 1500
# tokens leaves comfortable room.
MAX_OUTPUT_TOKENS = 1_500

# Hard cap on input — same 200K cover as cognify_extract / fanout.
INPUT_CAP_CHARS = 200_000


@dataclass
class ResummarizeResult:
    sessions_discovered: int = 0
    sessions_processed: int = 0
    sessions_failed: int = 0
    llm_calls: int = 0
    failure_counts: dict = field(default_factory=dict)
    dry_run: bool = False


def discover_sessions_needing_resummarize(
    conn: Any,
    *,
    project_id: Any = None,
    since: datetime | None = None,
    limit: int | None = None,
    settled_after_minutes: int = SETTLED_AFTER_MINUTES,
) -> list[tuple[str, str | None]]:
    """raw_sessions that need a Sonnet summary upgrade.

    Returns list of (raw_session_id, project_id) tuples. A session
    qualifies when ALL of:
      * has chunks of source_type='session_summary' (Ollama already ran)
      * summary_source IS NULL OR = 'ollama' (not yet upgraded)
      * created_at is older than `settled_after_minutes` minutes
        (settled — no chance of mid-conversation upgrade)
      * NO matching row in devbrain.end_session_log (agent never wrapped up)
    """
    where = [
        "EXISTS (SELECT 1 FROM devbrain.chunks c "
        "        WHERE c.source_id = rs.id "
        "          AND c.source_type = 'session_summary')",
        "(rs.summary_source IS NULL OR rs.summary_source = 'ollama')",
        f"rs.created_at < NOW() - INTERVAL '{int(settled_after_minutes)} minutes'",
        # end_session_log keys on session_id (TEXT) — the agent-provided
        # value. raw_sessions.session_id is the same shape. No entry → never
        # cleanly ended.
        "NOT EXISTS (SELECT 1 FROM devbrain.end_session_log esl "
        "            WHERE esl.session_id = rs.session_id)",
    ]
    params: list = []

    if project_id is not None:
        where.append("rs.project_id = %s")
        params.append(project_id)
    if since is not None:
        where.append("rs.created_at >= %s")
        params.append(since)

    sql = (
        "SELECT rs.id::text, rs.project_id::text "
        "FROM devbrain.raw_sessions rs "
        "WHERE " + " AND ".join(where) + " "
        # Oldest-orphan-first — these are the rows that have been
        # waiting longest for an upgrade. Helps the operator-facing
        # "how stale is the backlog" question.
        "ORDER BY rs.created_at ASC"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [(r[0], r[1]) for r in cur.fetchall()]


def _load_session_raw_content(conn: Any, session_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT raw_content FROM devbrain.raw_sessions WHERE id = %s",
            (session_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _build_summarize_prompt(raw_content: str) -> tuple[str, str]:
    """Returns (system_prompt, user_message). Same structural intent as
    ingest/summarize.py's Ollama prompt — focus on what was accomplished,
    decisions made, files touched, issues + resolution, lessons —
    but written for Sonnet's stronger reasoning."""
    sys_prompt = (
        "You are summarizing a developer's AI-coding session transcript. "
        "Produce a focused summary covering:\n"
        "  - What was accomplished (concrete deliverables, by file/PR if visible)\n"
        "  - Key decisions made and why (rationale, alternatives considered)\n"
        "  - Files created or modified\n"
        "  - Issues encountered and how they were resolved\n"
        "  - Important patterns or lessons learned\n"
        "  - Open threads / next steps if discussed\n\n"
        "Be specific about file names, function names, PR numbers, error "
        "messages — these are the searchable handles a future query will "
        "use. Keep the summary 200–800 words. Write in third person past "
        "tense ('the agent did X', 'they decided Y') for consistency.\n\n"
        "Return ONLY the summary text. No preamble, no markdown headers, "
        "no commentary about what you're doing."
    )
    truncated = raw_content[:INPUT_CAP_CHARS]
    user_msg = (
        "Summarize this session transcript per the instructions.\n\n"
        f"<<< SESSION CONTENT >>>\n{truncated}"
    )
    return sys_prompt, user_msg


def _call_sonnet(
    raw_content: str, model: str,
) -> tuple[str | None, dict, str | None]:
    """Returns (summary_text, usage_dict, failure_label).

    failure_label values:
      None        — success
      'no_auth'   — credential not configured
      'api'       — API call errored
      'empty'     — empty response
    """
    empty_usage = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        return None, empty_usage, "no_auth"

    from cognify.extract import _resolve_auth  # noqa: PLC0415

    auth_kwargs = _resolve_auth()
    if auth_kwargs is None:
        return None, empty_usage, "no_auth"

    sys_text, user_text = _build_summarize_prompt(raw_content)

    from cognify._anthropic_auth import claude_code_system_prefix  # noqa: PLC0415

    system_blocks: list[dict[str, Any]] = []
    prefix = claude_code_system_prefix()
    if prefix:
        system_blocks.append({"type": "text", "text": prefix})
    system_blocks.append({
        "type": "text", "text": sys_text,
        "cache_control": {"type": "ephemeral"},
    })

    client = anthropic.Anthropic(**auth_kwargs)
    try:
        response = client.messages.create(
            model=model, max_tokens=MAX_OUTPUT_TOKENS,
            system=system_blocks,
            messages=[{"role": "user", "content": user_text}],
        )
    except Exception as exc:  # noqa: BLE001
        return None, empty_usage, f"api:{type(exc).__name__}"

    usage = getattr(response, "usage", None)
    usage_dict = {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    } if usage else empty_usage

    text = "".join(
        b.text for b in (response.content or [])
        if getattr(b, "type", None) == "text"
    ).strip()
    if not text:
        return None, usage_dict, "empty"
    return text, usage_dict, None


def _replace_session_summary(
    conn: Any,
    *,
    session_id: str,
    project_id: str | None,
    new_summary: str,
    source: str,
) -> None:
    """Update raw_sessions.summary + summary_source AND replace the
    session_summary chunks (delete + re-embed + insert) so vector search
    picks up the new content immediately.

    Borrowed from ingest/pipeline.py::_summarize_session — kept inline
    here so the cognify pass doesn't have a runtime dep on the ingest
    package (factory/ and ingest/ are intentionally separate).
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.raw_sessions "
            "SET summary = %s, summary_source = %s "
            "WHERE id = %s",
            (new_summary, source, session_id),
        )
        # Delete the existing session_summary chunks for this session.
        cur.execute(
            "DELETE FROM devbrain.chunks "
            "WHERE source_id = %s AND source_type = 'session_summary'",
            (session_id,),
        )

    # Re-chunk + re-embed the new summary.
    summary_chunks = _chunk_text(new_summary)
    embeddings = [_embed_via_ollama(c["content"]) for c in summary_chunks]

    # Insert via direct SQL.
    with conn.cursor() as cur:
        for chunk, emb in zip(summary_chunks, embeddings):
            vector_str = f"[{','.join(str(v) for v in emb)}]"
            cur.execute(
                """
                INSERT INTO devbrain.chunks
                    (project_id, source_type, source_id,
                     source_line_start, source_line_end,
                     content, embedding, token_count)
                VALUES (%s, 'session_summary', %s, %s, %s, %s, %s::vector, %s)
                """,
                (
                    project_id, session_id,
                    chunk["line_start"], chunk["line_end"],
                    chunk["content"], vector_str, chunk["token_count"],
                ),
            )
    conn.commit()


# ─── Inline helpers (avoid sys.path collision with ingest's `config`) ───────

# Mirror of ingest/chunker.py constants. Default from config/devbrain.yaml is
# max_tokens: 400, overlap_tokens: 80; embed at 4 chars/token (conservative).
_CHUNK_MAX_TOKENS = 400
_CHUNK_OVERLAP_TOKENS = 80
_CHARS_PER_TOKEN = 4


def _chunk_text(text: str) -> list[dict]:
    """Line-boundary chunker matching ingest/chunker.py's shape.

    Returns list of {content, line_start, line_end, token_count} dicts.
    """
    lines = text.split("\n")
    max_chars = _CHUNK_MAX_TOKENS * _CHARS_PER_TOKEN
    overlap_chars = _CHUNK_OVERLAP_TOKENS * _CHARS_PER_TOKEN

    chunks: list[dict] = []
    current_lines: list[str] = []
    current_chars = 0
    chunk_start_line = 0

    for i, line in enumerate(lines):
        line_len = len(line) + 1
        current_lines.append(line)
        current_chars += line_len

        if current_chars >= max_chars:
            content = "\n".join(current_lines)
            chunks.append({
                "content": content,
                "line_start": chunk_start_line,
                "line_end": i,
                "token_count": len(content) // _CHARS_PER_TOKEN,
            })
            # Overlap window.
            overlap_lines: list[str] = []
            overlap_total = 0
            for prev in reversed(current_lines):
                overlap_total += len(prev) + 1
                overlap_lines.insert(0, prev)
                if overlap_total >= overlap_chars:
                    break
            current_lines = overlap_lines
            current_chars = overlap_total
            chunk_start_line = max(0, i - len(overlap_lines) + 1)

    if current_lines:
        content = "\n".join(current_lines)
        chunks.append({
            "content": content,
            "line_start": chunk_start_line,
            "line_end": len(lines) - 1,
            "token_count": len(content) // _CHARS_PER_TOKEN,
        })
    return chunks


def _embed_via_ollama(text: str) -> list[float]:
    """Embed via the same Ollama endpoint the ingest pipeline uses."""
    import json as _json  # noqa: PLC0415
    import os  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = os.environ.get("DEVBRAIN_OLLAMA_URL", "http://localhost:11434")
    model = os.environ.get("DEVBRAIN_EMBEDDING_MODEL", "snowflake-arctic-embed2")
    data = _json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(
        f"{url}/api/embed",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = _json.loads(resp.read())
    return payload["embeddings"][0]


def run_resummarize(
    conn: Any,
    *,
    project_id: Any = None,
    since: datetime | None = None,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    max_sessions: int | None = None,
    progress_callback=None,
) -> ResummarizeResult:
    """Discover + upgrade orphan-of-end_session sessions to Sonnet summaries.

    Idempotent against the summary_source marker — re-running won't
    re-summarize already-upgraded sessions. Safe to interrupt at any
    point; the next run picks up unfinished work.
    """
    from observability.pricing import (  # noqa: PLC0415
        get_pricing, compute_cost_usd, SONNET_4_6,
    )
    from observability.spend import record_spend  # noqa: PLC0415

    result = ResummarizeResult(dry_run=dry_run)
    pricing = get_pricing(model) or SONNET_4_6

    targets = discover_sessions_needing_resummarize(
        conn, project_id=project_id, since=since, limit=max_sessions,
    )
    result.sessions_discovered = len(targets)

    if dry_run or not targets:
        return result

    for idx, (session_id, sess_project_id) in enumerate(targets, start=1):
        raw_content = _load_session_raw_content(conn, session_id)
        if raw_content is None or not raw_content.strip():
            result.sessions_failed += 1
            result.failure_counts["no_content"] = (
                result.failure_counts.get("no_content", 0) + 1
            )
            if progress_callback:
                progress_callback(idx, len(targets), {
                    "session_id": session_id,
                    "failure": "no_content",
                })
            continue

        summary, usage, failure = _call_sonnet(raw_content, model)

        # Record spend regardless of outcome (we paid for the call).
        if any(usage.get(k, 0) for k in ("input_tokens", "output_tokens")):
            cost = compute_cost_usd(
                pricing,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_read_tokens=usage["cache_read_tokens"],
                cache_write_tokens=usage["cache_write_tokens"],
            )
            try:
                record_spend(
                    conn, project_id=sess_project_id,
                    pass_name="resummarize", model=model,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    cache_read_tokens=usage["cache_read_tokens"],
                    cache_write_tokens=usage["cache_write_tokens"],
                    cost_usd=cost,
                )
            except Exception:  # noqa: BLE001
                logger.exception("resummarize: record_spend failed")
            result.llm_calls += 1

        if failure:
            result.sessions_failed += 1
            result.failure_counts[failure] = (
                result.failure_counts.get(failure, 0) + 1
            )
            if progress_callback:
                progress_callback(idx, len(targets), {
                    "session_id": session_id,
                    "failure": failure,
                })
            continue

        try:
            _replace_session_summary(
                conn,
                session_id=session_id,
                project_id=sess_project_id,
                new_summary=summary or "",
                source="sonnet" if model.startswith("claude-sonnet")
                       else "opus" if model.startswith("claude-opus")
                       else "anthropic",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("resummarize: replace failed for %s", session_id)
            result.sessions_failed += 1
            result.failure_counts["replace_error"] = (
                result.failure_counts.get("replace_error", 0) + 1
            )
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            if progress_callback:
                progress_callback(idx, len(targets), {
                    "session_id": session_id,
                    "failure": "replace_error",
                    "detail": str(exc)[:200],
                })
            continue

        result.sessions_processed += 1
        if progress_callback:
            progress_callback(idx, len(targets), {
                "session_id": session_id,
                "failure": None,
            })

    return result


# ─── Pass registration ──────────────────────────────────────────────────────


def _register_resummarize_pass() -> None:
    """Late-bound registration with the cognify orchestrator. Imported
    lazily by orchestrator._ensure_registry()."""
    from cognify.orchestrator import CognifyPass, PassResult, register_pass

    @register_pass
    class ResummarizePass(CognifyPass):
        pass_name = "resummarize"

        def run(self, conn, project_id, *, dry_run=False) -> "PassResult":
            res = run_resummarize(conn, project_id=project_id, dry_run=dry_run)
            return PassResult(
                rows_processed=res.sessions_processed,
                llm_calls=res.llm_calls,
                metadata={
                    "pass": "resummarize",
                    "sessions_discovered": res.sessions_discovered,
                    "sessions_failed": res.sessions_failed,
                    "failure_counts": res.failure_counts,
                },
                dry_run=dry_run,
            )

    return ResummarizePass


_register_resummarize_pass()


__all__ = [
    "DEFAULT_MODEL",
    "SETTLED_AFTER_MINUTES",
    "ResummarizeResult",
    "discover_sessions_needing_resummarize",
    "run_resummarize",
]

"""cognify_resummarize — upgrade Ollama summaries for orphan sessions.

The ingest watcher writes an initial session_summary at ingest time using
local Ollama (qwen2.5:7b — free, lower quality). Most sessions then get
an agent-curated summary via end_session, which supersedes the Ollama
version in deep_search ranking.

For sessions where end_session was NEVER called (long-running automation,
abandoned conversations, mid-stream errors), the Ollama summary stays
the canonical row — and search results degrade accordingly.

This pass closes that gap: it scans for raw_sessions whose summary_source
is 'ollama' (or NULL — pre-migration-041) AND has no end_session_log
entry AND the conversation is settled. For each, it re-summarizes with a
hosted model, replaces the summary + session_summary chunks, and marks
summary_source ('codex', 'sonnet', 'openai', ...) so the pass is
idempotent.

Backends: the same order fanout uses. Codex first (the isolated CODEX_HOME,
model cognify.codex_model, ChatGPT login — no per-token spend), then the
Anthropic API only when a credential exists; an explicit cognify.backend
means that backend only. cognify.allowed_backends is honoured for each.
Which backends are usable is decided ONCE per run: with none (no codex
login, no Anthropic credential, or a policy refusal) the run records
``blocked`` and attempts nothing, instead of failing every session.

Per-run cap: MAX_SESSIONS_PER_RUN (env DEVBRAIN_RESUMMARIZE_MAX_PER_RUN,
default 20) so a backlog drains in batches; ``sessions_discovered`` keeps
the full backlog size for monitoring.

Cost on the Anthropic fallback: ~$0.01–0.02 per session (Sonnet, ~9K avg
input tokens).

Schedule: 60-min launchd cadence; manual trigger via
`devbrain cognify --pass=resummarize`.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Primary model. "codex" routes to the codex CLI with cognify.codex_model;
# a claude-* id routes to the Anthropic API (no codex attempt). Same
# convention as DEVBRAIN_FANOUT_MODEL.
DEFAULT_MODEL = os.environ.get("DEVBRAIN_RESUMMARIZE_MODEL", "codex")

# Anthropic model used when codex is unavailable or fails (model-based
# routing only). "none" disables the fallback.
FALLBACK_MODEL = os.environ.get("DEVBRAIN_RESUMMARIZE_FALLBACK", "claude-sonnet-5")

# Sessions attempted per run. The backlog drains in batches of this size.
MAX_SESSIONS_PER_RUN = int(os.environ.get("DEVBRAIN_RESUMMARIZE_MAX_PER_RUN", "20"))

# Stop the run after this many sessions in a row fail on every backend
# with an API error (quota, outage): the rest would fail the same way.
MAX_CONSECUTIVE_API_FAILURES = 3

# A session that failed this many runs goes to the back of the queue, so
# a few bad sessions (empty transcript, one that always times out) cannot
# block the oldest-first batch forever.
MAX_SESSION_TRIES = 3

# Failures that mean a backend cannot be used at all this run.
_DEAD_BACKEND_FAILURES = ("no_auth", "refused", "unavailable")

# codex --output-schema for the summary (strict structured output).
_CODEX_SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary"],
    "properties": {"summary": {"type": "string"}},
}

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
    sessions_attempted: int = 0
    # Set when the run attempted nothing because no backend was usable:
    # "refused" (policy) or "no_auth" / "unavailable".
    blocked: str | None = None
    # {backend: "code: detail"} for backends that could not be used.
    backend_status: dict = field(default_factory=dict)
    backends_used: dict = field(default_factory=dict)
    aborted: str | None = None
    # {raw_session_id: failed runs} carried to the next run (ids only).
    failed_tries: dict = field(default_factory=dict)


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
    if not row or row[0] is None:
        return None
    from cognify import screen  # noqa: PLC0415

    # Withhold injection-flagged messages before the summarizer sees them.
    return screen.withhold_usf(row[0])[0]


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
      'refused'   — anthropic not in cognify.allowed_backends
      'api'       — API call errored
      'empty'     — empty response
    """
    empty_usage = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    from cognify import backends  # noqa: PLC0415

    if not backends.check("anthropic", "resummarize"):
        return None, empty_usage, "refused"
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


def _call_openai(
    raw_content: str, model: str,
) -> tuple[str | None, dict, str | None]:
    """The summary on the OpenAI platform API (cognify.backend=openai).
    Same return contract as _call_sonnet."""
    from cognify import openai_api  # noqa: PLC0415

    sys_text, user_text = _build_summarize_prompt(raw_content)
    res = openai_api.complete(
        sys_text, user_text,
        pass_name="resummarize",
        model=model,
        max_output_tokens=8_000,
    )
    if res.failure:
        label = res.failure if res.failure in ("refused", "no_auth", "empty") \
            else f"{res.failure}:openai"
        return None, res.usage, label
    return res.text, res.usage, None


def _empty_usage() -> dict:
    return {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }


def _call_codex(
    raw_content: str, model: str | None,
) -> tuple[str | None, dict, str | None]:
    """The summary via `codex exec` on the isolated CODEX_HOME (no token
    spend). Same return contract as _call_sonnet. ``model`` is used only
    when it is a codex/OpenAI id; otherwise cognify.codex_model."""
    from cognify import backends, codex_exec  # noqa: PLC0415
    from cognify.extract import _routes_to_codex  # noqa: PLC0415

    sys_text, user_text = _build_summarize_prompt(raw_content)
    prompt = (
        sys_text
        + "\n\nPut the summary text in the `summary` field.\n\n"
        + user_text
    )
    parsed, failure, detail = codex_exec.run_json(
        prompt,
        _CODEX_SUMMARY_SCHEMA,
        pass_name="resummarize",
        required_keys=("summary",),
        model=model if _routes_to_codex(model) else None,
    )
    if failure:
        if failure not in _DEAD_BACKEND_FAILURES:
            logger.warning("resummarize(codex): %s: %s", failure, (detail or "")[:200])
        return None, _empty_usage(), backends.failure_label(failure, detail)
    text = str((parsed or {}).get("summary") or "").strip()
    if not text:
        return None, _empty_usage(), "empty"
    return text, _empty_usage(), None


def _anthropic_model(model: str) -> str:
    """The Anthropic model for a call whose primary ``model`` may be a
    codex id: the primary when it is an Anthropic id, else the fallback."""
    from cognify.extract import _ANTHROPIC_DEFAULT_MODEL, _routes_to_codex  # noqa: PLC0415

    if model and not _routes_to_codex(model):
        return model
    if FALLBACK_MODEL and not _routes_to_codex(FALLBACK_MODEL) \
            and FALLBACK_MODEL.lower() != "none":
        return FALLBACK_MODEL
    return _ANTHROPIC_DEFAULT_MODEL


@dataclass
class _Attempt:
    text: str | None
    usage: dict
    failure: str | None
    backend: str | None  # the backend that produced text (or failed last)
    model: str | None
    calls: int  # backend invocations that reached a model
    dead: dict = field(default_factory=dict)  # backend -> failure, removed


def _summarize_with(raw_content: str, model: str, order: list[str]) -> _Attempt:
    """Try ``order`` in turn until one returns a summary.

    A backend that answers no_auth / refused / unavailable is REMOVED from
    ``order`` (the caller's list), so the rest of the run skips it."""
    last = _Attempt(None, _empty_usage(), "refused", None, None, 0)
    calls = 0
    dead_backends: dict[str, str] = {}
    for backend in list(order):
        if backend == "codex":
            used_model = model
            text, usage, failure = _call_codex(raw_content, model)
        elif backend == "anthropic":
            used_model = _anthropic_model(model)
            text, usage, failure = _call_sonnet(raw_content, used_model)
        elif backend == "openai":
            from cognify import openai_api  # noqa: PLC0415

            used_model = openai_api.model_for(model)
            text, usage, failure = _call_openai(raw_content, used_model)
        else:
            from cognify import backends  # noqa: PLC0415

            backends.check(backend, "resummarize")  # logs the refusal
            used_model, text, usage, failure = None, None, _empty_usage(), "refused"
        dead = failure is not None and failure.split(":", 1)[0] in _DEAD_BACKEND_FAILURES
        if not dead:
            calls += 1
        if dead:
            dead_backends[backend] = failure
            if backend in order:
                order.remove(backend)
        last = _Attempt(text, usage, failure, backend, used_model, calls, dead_backends)
        if failure is None:
            return last
    return last


def _summarize(
    raw_content: str, model: str,
) -> tuple[str | None, dict, str | None]:
    """One summary through this deployment's backend chain (see
    backends.chain). Returns (text, usage, failure)."""
    from cognify import backends  # noqa: PLC0415

    order = backends.chain(model, FALLBACK_MODEL)
    a = _summarize_with(raw_content, model, order)
    return a.text, a.usage, a.failure


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
    """Discover + upgrade orphan-of-end_session sessions' summaries.

    Attempts at most ``max_sessions`` (default MAX_SESSIONS_PER_RUN)
    sessions, oldest first; ``sessions_discovered`` is the whole backlog.
    Idempotent against the summary_source marker — re-running won't
    re-summarize already-upgraded sessions. Safe to interrupt at any
    point; the next run picks up unfinished work.
    """
    from observability.pricing import (  # noqa: PLC0415
        get_pricing, compute_cost_usd, SONNET_4_6,
    )
    from observability.spend import record_spend  # noqa: PLC0415
    from cognify import backends  # noqa: PLC0415

    result = ResummarizeResult(dry_run=dry_run)
    cap = MAX_SESSIONS_PER_RUN if max_sessions is None else max(0, int(max_sessions))

    targets = discover_sessions_needing_resummarize(
        conn, project_id=project_id, since=since,
    )
    result.sessions_discovered = len(targets)
    tries = _last_failed_tries(conn, project_id) if targets and not dry_run else {}
    if tries:
        waiting = {sid for sid, _ in targets}
        result.failed_tries = {k: v for k, v in tries.items() if k in waiting}
        # Stable: oldest-first within each group.
        targets.sort(key=lambda t: result.failed_tries.get(t[0], 0) >= MAX_SESSION_TRIES)

    if dry_run or not targets or cap == 0:
        return result

    # Decide once which backends this run can use. With none, attempt
    # nothing: the old per-session check failed every session of the
    # backlog with the same no_auth, every hour (859 a run on the Studio).
    order, result.backend_status = backends.usable(
        backends.chain(model, FALLBACK_MODEL), "resummarize"
    )
    if not order:
        result.blocked = backends.blocked_label(result.backend_status)
        logger.warning(
            "resummarize: no usable backend (%s) — %d session(s) waiting",
            "; ".join(f"{b}: {r}" for b, r in result.backend_status.items()) or "none",
            len(targets),
        )
        return result

    batch = targets[:cap]
    api_streak = 0
    for idx, (session_id, sess_project_id) in enumerate(batch, start=1):
        if not order:
            # Every backend died mid-run (e.g. the codex login was revoked).
            result.blocked = backends.blocked_label(result.backend_status)
            break
        if api_streak >= MAX_CONSECUTIVE_API_FAILURES:
            result.aborted = "api_failures"
            logger.warning(
                "resummarize: %d sessions in a row failed with API errors; "
                "stopping this run", api_streak,
            )
            break
        result.sessions_attempted += 1
        raw_content = _load_session_raw_content(conn, session_id)
        if raw_content is None or not raw_content.strip():
            result.sessions_failed += 1
            result.failed_tries[session_id] = result.failed_tries.get(session_id, 0) + 1
            result.failure_counts["no_content"] = (
                result.failure_counts.get("no_content", 0) + 1
            )
            if progress_callback:
                progress_callback(idx, len(batch), {
                    "session_id": session_id,
                    "failure": "no_content",
                })
            continue

        attempt = _summarize_with(raw_content, model, order)
        for gone, why in attempt.dead.items():
            result.backend_status[gone] = f"{why}: failed mid-run"
        result.llm_calls += attempt.calls
        usage = attempt.usage

        # Record spend regardless of outcome (we paid for the call).
        if any(usage.get(k, 0) for k in ("input_tokens", "output_tokens")):
            spend_model = attempt.model or model
            cost = compute_cost_usd(
                get_pricing(spend_model) or SONNET_4_6,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_read_tokens=usage["cache_read_tokens"],
                cache_write_tokens=usage["cache_write_tokens"],
            )
            try:
                record_spend(
                    conn, project_id=sess_project_id,
                    pass_name="resummarize", model=spend_model,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    cache_read_tokens=usage["cache_read_tokens"],
                    cache_write_tokens=usage["cache_write_tokens"],
                    cost_usd=cost,
                )
            except Exception:  # noqa: BLE001
                logger.exception("resummarize: record_spend failed")

        failure = attempt.failure
        if failure:
            result.sessions_failed += 1
            if failure.split(":", 1)[0] not in _DEAD_BACKEND_FAILURES:
                # The session's own fault (or a transient API error), not a
                # missing backend: count it toward deprioritising.
                result.failed_tries[session_id] = result.failed_tries.get(session_id, 0) + 1
            result.failure_counts[failure] = (
                result.failure_counts.get(failure, 0) + 1
            )
            if failure.split(":", 1)[0] == "api":
                api_streak += 1
            if progress_callback:
                progress_callback(idx, len(batch), {
                    "session_id": session_id,
                    "failure": failure,
                })
            continue
        api_streak = 0

        try:
            _replace_session_summary(
                conn,
                session_id=session_id,
                project_id=sess_project_id,
                new_summary=attempt.text or "",
                source=_summary_source(attempt.backend, attempt.model),
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
                progress_callback(idx, len(batch), {
                    "session_id": session_id,
                    "failure": "replace_error",
                    "detail": str(exc)[:200],
                })
            continue

        result.sessions_processed += 1
        result.failed_tries.pop(session_id, None)
        result.backends_used[attempt.backend] = (
            result.backends_used.get(attempt.backend, 0) + 1
        )
        if progress_callback:
            progress_callback(idx, len(batch), {
                "session_id": session_id,
                "failure": None,
            })

    if not order and result.blocked is None:
        result.blocked = backends.blocked_label(result.backend_status)
    return result


def _last_failed_tries(conn: Any, project_id: Any) -> dict[str, int]:
    """{raw_session_id: failed runs} from the last completed real run."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT metadata -> 'failed_tries' FROM devbrain.cognify_run_log "
            "WHERE pass_name = 'resummarize' "
            "  AND project_id IS NOT DISTINCT FROM %s "
            "  AND completed_at IS NOT NULL AND error IS NULL "
            "  AND coalesce(metadata ->> 'dry_run', 'false') <> 'true' "
            "ORDER BY started_at DESC LIMIT 1",
            (project_id,),
        )
        row = cur.fetchone()
    raw = row[0] if row and isinstance(row[0], dict) else {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _summary_source(backend: str | None, model: str | None) -> str:
    """raw_sessions.summary_source for a summary from ``backend``."""
    if backend in ("codex", "openai"):
        return backend
    m = (model or "").lower()
    if m.startswith("claude-sonnet"):
        return "sonnet"
    if m.startswith("claude-opus"):
        return "opus"
    return "anthropic"


# ─── Pass registration ──────────────────────────────────────────────────────


def _register_resummarize_pass() -> None:
    """Late-bound registration with the cognify orchestrator. Imported
    lazily by orchestrator._ensure_registry()."""
    from cognify.orchestrator import CognifyPass, PassResult, register_pass

    @register_pass
    class ResummarizePass(CognifyPass):
        pass_name = "resummarize"

        def run(self, conn, project_id, *, dry_run=False) -> "PassResult":
            from cognify import backends  # noqa: PLC0415

            res = run_resummarize(conn, project_id=project_id, dry_run=dry_run)
            metadata = {
                "pass": "resummarize",
                "sessions_discovered": res.sessions_discovered,
                "sessions_attempted": res.sessions_attempted,
                "sessions_processed": res.sessions_processed,
                "sessions_failed": res.sessions_failed,
                "backlog_remaining": res.sessions_discovered - res.sessions_processed,
                "failure_counts": res.failure_counts,
                "max_per_run": MAX_SESSIONS_PER_RUN,
                "backends_used": res.backends_used,
            }
            if res.blocked:
                metadata["blocked"] = res.blocked
                metadata["allowed_backends"] = sorted(backends.allowed())
            if res.backend_status:
                metadata["backend_status"] = res.backend_status
            if res.aborted:
                metadata["aborted"] = res.aborted
            if res.failed_tries:
                # Read back by the next run (_last_failed_tries). Ids only.
                metadata["failed_tries"] = res.failed_tries
            return PassResult(
                rows_processed=res.sessions_processed,
                llm_calls=res.llm_calls,
                metadata=metadata,
                dry_run=dry_run,
            )

    return ResummarizePass


_register_resummarize_pass()


__all__ = [
    "DEFAULT_MODEL",
    "FALLBACK_MODEL",
    "MAX_SESSIONS_PER_RUN",
    "SETTLED_AFTER_MINUTES",
    "ResummarizeResult",
    "discover_sessions_needing_resummarize",
    "run_resummarize",
]

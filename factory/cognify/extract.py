"""cognify_extract — lesson + decision extraction from ingested sessions.

This module provides:
  1. ``run_extract_pass(conn, project_id, ...)`` — the scheduled pass that
     processes all sessions ingested since the last successful extract run.
  2. ``extract_from_session(conn, session_id, project_id, ...)`` — single-
     session extraction (used by both the scheduled pass and reextract_cli).

The extraction logic (LLM call to produce structured lessons/decisions from
raw session content) lives here. factory/curator/end_session.py delegates
to this module via ``run_extract_pass`` for any scheduled extraction; the
end_session orchestration shell remains in end_session.py.

Idempotency: (provenance_id, kind) pairs already present in devbrain.memory
are skipped. Re-running on the same session is a no-op unless content changed.

PHI constraint: LLM prompts contain session content (which may be sensitive),
but the cognify_run_log rows written by the orchestrator never receive raw
memory.content — only row counts and metadata are logged.

LLM cost ceiling: max 20 calls per pass invocation (Sonnet 4.6).

Spend tracking: each LLM call writes a row to devbrain.cognify_spend_log
(migration 029) via observability.spend.record_spend. Costs are estimates
based on hardcoded Anthropic list prices (see observability/pricing.py).

Versioned extraction: CURRENT_EXTRACTION_VERSION marks the prompt/model
version for all newly written tier='lesson' rows. Bump this constant when
the extraction prompt or model changes, then run:
    devbrain cognify-reextract --since-version=<N>
to reprocess rows produced by prior versions.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from cognify.orchestrator import CognifyPass, PassResult, register_pass
from observability.pricing import SONNET_4_6, compute_cost_usd
from observability.spend import record_spend

logger = logging.getLogger(__name__)

# Maximum LLM calls per scheduled pass invocation.
MAX_LLM_CALLS_PER_PASS = 20

# Retry configuration for transient APIConnectionError. The 2026-05-14
# marathon-reextract incident showed that long-running cognify processes
# accumulate stale connections in httpx's internal pool; per-call retries
# with client recycle defeat that failure mode. Other exception classes
# (auth, 400, JSONDecodeError) are NOT retried — they won't improve.
_API_MAX_RETRIES = 3
_API_BACKOFF_S = (2, 5, 15)  # exponential, applied in order

# Extraction prompt/model version. Bump this integer when the extraction
# prompt or model changes, then run `devbrain cognify-reextract --since-version=N`
# to reprocess existing rows. Starts at 1 per Phase 6 design §6.
CURRENT_EXTRACTION_VERSION = 1

# Model used for extraction. Must match a key in observability/pricing.py.
_EXTRACT_MODEL = "claude-sonnet-4-6"


@dataclass
class ExtractResult:
    """Result of extracting lessons/decisions from a single session.

    `lessons_created` is the LLM-facing nomenclature — the prompt asks
    the model to produce "lessons" and we count what it returned. In
    DB storage these rows land with `kind='pattern'` (the storage-layer
    naming); see `_upsert_memory(..., kind='pattern', ...)` below. The
    `patterns_created` attribute is an alias for the same int, exposed
    so SQL-side code can use the storage name without remembering the
    LLM-side rename.
    """

    session_id: str
    lessons_created: int = 0
    decisions_created: int = 0
    skipped_duplicates: int = 0
    llm_calls: int = 0
    memory_ids: list[str] = field(default_factory=list)
    failure: str | None = None  # None | "api" | "json_parse" | "empty"

    @property
    def patterns_created(self) -> int:
        """Alias for lessons_created — matches DB storage kind ('pattern')."""
        return self.lessons_created


@register_pass
class ExtractPass(CognifyPass):
    """cognify_extract: lesson/decision extraction from recent sessions.

    Hourly pass via launchd. Up to 20 LLM calls per run.
    """

    pass_name = "extract"

    def run(
        self,
        conn: Any,
        project_id: Any,
        *,
        dry_run: bool = False,
        max_llm_calls: int | None = None,
    ) -> PassResult:
        """Extract lessons/decisions from sessions ingested since last pass.

        Args:
            conn: psycopg2 connection.
            project_id: UUID. Required for extract (LLM pass; project-scoped).
            dry_run: if True, compute candidate sessions without extracting.
            max_llm_calls: per-pass ceiling on LLM calls. When None (default),
                uses MAX_LLM_CALLS_PER_PASS=20.

        Returns:
            PassResult with row counts.
        """
        if project_id is None:
            raise ValueError(
                "cognify_extract requires a project_id "
                "(it's an LLM-cost pass; always project-scoped)"
            )

        cap = max_llm_calls if max_llm_calls is not None else MAX_LLM_CALLS_PER_PASS

        since = _last_successful_run(conn, "extract", project_id)
        candidate_sessions = _sessions_since(conn, project_id, since)

        if dry_run:
            return PassResult(
                rows_processed=0,
                llm_calls=0,
                metadata={
                    "pass": "extract",
                    "dry_run_candidate_sessions": len(candidate_sessions),
                    "since": since.isoformat() if since else None,
                },
            )

        total_lessons = 0
        total_decisions = 0
        total_llm = 0
        sessions_processed = 0
        failure_counts: dict[str, int] = {}

        for session_id in candidate_sessions:
            if total_llm >= cap:
                logger.info(
                    "cognify_extract: LLM cap (%d) reached, deferring %d sessions",
                    cap,
                    len(candidate_sessions) - sessions_processed,
                )
                break
            result = extract_from_session(
                conn,
                session_id,
                project_id,
                max_llm_calls=cap - total_llm,
            )
            total_lessons += result.lessons_created
            total_decisions += result.decisions_created
            total_llm += result.llm_calls
            sessions_processed += 1
            if result.failure:
                failure_counts[result.failure] = failure_counts.get(result.failure, 0) + 1

        rows = total_lessons + total_decisions
        return PassResult(
            rows_processed=rows,
            llm_calls=total_llm,
            metadata={
                "pass": "extract",
                "sessions_processed": sessions_processed,
                # LLM-side naming (what the model produces); DB stores these
                # under `kind='pattern'`, so `patterns_created` is an alias.
                "lessons_created": total_lessons,
                "patterns_created": total_lessons,
                "decisions_created": total_decisions,
                # A4: surface per-failure-kind counts so silent JSON parse
                # errors and API failures are visible in the run log.
                "failure_counts": failure_counts,
                "since": since.isoformat() if since else None,
            },
        )


def run_extract_pass(
    conn: Any,
    project_id: Any,
    *,
    dry_run: bool = False,
) -> PassResult:
    """Convenience wrapper used by end_session.py and the reextract CLI.

    Delegates to ExtractPass.run().
    """
    return ExtractPass().run(conn, project_id, dry_run=dry_run)


def extract_from_session(
    conn: Any,
    session_id: str,
    project_id: Any,
    *,
    max_llm_calls: int = MAX_LLM_CALLS_PER_PASS,
    reextract: bool = False,
) -> ExtractResult:
    """Extract lessons and decisions from a single session's memory chunks.

    Args:
        conn: psycopg2 connection.
        session_id: provenance_id of the session to extract from.
        project_id: UUID. Used to scope the memory INSERT and validate
            isolation.
        max_llm_calls: upper bound on LLM calls this function may make.
        reextract: if True, archive existing lessons/decisions for this
            session before re-extracting (for reextract_cli).

    Returns:
        ExtractResult with counts.
    """
    if reextract:
        _archive_prior_extracts(conn, session_id, project_id)

    # Fetch raw chunks for this session.
    chunks = _load_session_chunks(conn, session_id, project_id)
    if not chunks:
        logger.debug(
            "cognify_extract: session %s has no chunks to extract from",
            session_id,
        )
        return ExtractResult(session_id=session_id)

    # Build combined content for the LLM.
    combined = _combine_chunks(chunks)
    if not combined.strip():
        return ExtractResult(session_id=session_id)

    # Call LLM for extraction.
    extracted = _llm_extract(combined, max_llm_calls=max_llm_calls)
    llm_calls = 1  # one call per session for v1

    # Record spend for this LLM call.
    usage = extracted.get("_usage", {})
    if any(usage.get(k, 0) for k in ("input_tokens", "output_tokens")):
        cost = compute_cost_usd(
            SONNET_4_6,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
        )
        record_spend(
            conn,
            project_id=project_id,
            pass_name="extract",
            model=_EXTRACT_MODEL,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
            cost_usd=cost,
        )

    lessons_created = 0
    decisions_created = 0
    skipped = 0
    memory_ids: list[str] = []

    for item in extracted.get("lessons", []):
        mid, was_new = _upsert_memory(
            conn,
            project_id=project_id,
            kind="lesson",
            title=item.get("title", ""),
            content=item.get("content", ""),
            session_id=session_id,
            reextract=reextract,
            reextract_meta=item.get("reextracted_from"),
        )
        if was_new:
            lessons_created += 1
            memory_ids.append(str(mid))
        else:
            skipped += 1

    for item in extracted.get("decisions", []):
        mid, was_new = _upsert_memory(
            conn,
            project_id=project_id,
            kind="decision",
            title=item.get("title", ""),
            content=item.get("content", ""),
            session_id=session_id,
            reextract=reextract,
            reextract_meta=item.get("reextracted_from"),
        )
        if was_new:
            decisions_created += 1
            memory_ids.append(str(mid))
        else:
            skipped += 1

    return ExtractResult(
        session_id=session_id,
        lessons_created=lessons_created,
        decisions_created=decisions_created,
        skipped_duplicates=skipped,
        llm_calls=llm_calls,
        memory_ids=memory_ids,
        failure=extracted.get("_failure"),
    )


# ── Private helpers ───────────────────────────────────────────────────────────


def _last_successful_run(conn: Any, pass_name: str, project_id: Any):
    """Return the started_at of the most recent successful extract run, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT started_at FROM devbrain.cognify_run_log "
            "WHERE pass_name = %s AND project_id = %s AND error IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (pass_name, project_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _sessions_since(conn: Any, project_id: Any, since) -> list[str]:
    """Return provenance_ids (as strings) of sessions with chunks ingested after ``since``.

    If ``since`` is None, returns all sessions for the project.
    Sessions are ordered oldest-first so re-runs converge in order.
    """
    if since is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT provenance_id::text FROM devbrain.memory "
                "WHERE project_id = %s "
                "  AND provenance_id IS NOT NULL "
                "  AND archived_at IS NULL "
                "ORDER BY provenance_id::text",
                (project_id,),
            )
            return [r[0] for r in cur.fetchall()]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT provenance_id::text FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND provenance_id IS NOT NULL "
            "  AND archived_at IS NULL "
            "  AND created_at > %s "
            "ORDER BY provenance_id::text",
            (project_id, since),
        )
        return [r[0] for r in cur.fetchall()]


def _load_session_chunks(conn: Any, session_id: str, project_id: Any) -> list[dict]:
    """Load raw memory chunks for this session that aren't already extracts.

    We exclude tier='lesson' rows to avoid feeding the LLM its own prior
    extraction outputs.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, title, content FROM devbrain.memory "
            "WHERE provenance_id = %s::uuid "
            "  AND project_id = %s "
            "  AND archived_at IS NULL "
            "  AND tier != 'lesson' "
            "ORDER BY created_at",
            (session_id, project_id),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _combine_chunks(chunks: list[dict]) -> str:
    """Combine chunks into a single text block for LLM input."""
    parts = []
    for c in chunks:
        title = c.get("title") or ""
        content = c.get("content") or ""
        if title:
            parts.append(f"## {title}\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


def _llm_extract(content: str, *, max_llm_calls: int = 1) -> dict:
    """Call the LLM to extract structured lessons and decisions.

    Returns a dict with keys:
      - "lessons": list of {title, content}
      - "decisions": list of {title, content}
      - "_usage": dict with token usage fields for spend tracking
          (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens).
          Always present; all counts are 0 if the LLM was not called.

    v1 implementation: uses the Anthropic SDK with prompt caching.
    If the SDK is not available (e.g. CI without API key), returns empty
    lists so the pass degrades gracefully.
    """
    _empty_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        logger.warning("cognify_extract: anthropic SDK not available; skipping LLM")
        return {"lessons": [], "decisions": [], "_usage": _empty_usage}

    auth_kwargs = _resolve_auth()
    if auth_kwargs is None:
        logger.warning(
            "cognify_extract: no Anthropic credential configured "
            "(ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN); "
            "skipping LLM"
        )
        return {"lessons": [], "decisions": [], "_usage": _empty_usage}

    client = anthropic.Anthropic(**auth_kwargs)

    system_prompt = (
        "You are a structured knowledge extractor. Given raw session content, "
        "identify and extract:\n"
        "1. Lessons: generalizable insights or patterns worth remembering.\n"
        "2. Decisions: specific architectural or implementation choices made.\n\n"
        "Return JSON with keys 'lessons' and 'decisions', each a list of "
        "objects with 'title' (short, <= 80 chars) and 'content' (detailed). "
        "Return only the JSON, no preamble."
    )

    # OAuth path requires the Claude Code SDK fingerprint as the first
    # system block — without it /v1/messages 429s on subscription tokens.
    # Console API key path returns None and is unaffected.
    from cognify._anthropic_auth import claude_code_system_prefix
    system_blocks: list[dict[str, Any]] = []
    oauth_prefix = claude_code_system_prefix()
    if oauth_prefix:
        system_blocks.append({"type": "text", "text": oauth_prefix})
    system_blocks.append(
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    )

    # max_tokens=16000: stays under the SDK's ~10-min non-streaming timeout
    # guard while giving room for rich JSON output on long sessions. Sonnet
    # 4.6 supports 64K output but anything above ~16K requires streaming.
    #
    # Input cap of 200_000 chars (~50K tokens) — Sonnet 4.6 has a 1M context
    # window, but real session_summaries top out well below 200K chars. The
    # previous 8K cap silently discarded 95%+ of any non-trivial summary.
    # Make the LLM call. Distinguish three failure modes so callers can
    # tell whether a session produced 0 atoms because:
    #   _failure="api"        the API call itself errored (network/auth/429)
    #   _failure="json_parse" the model returned non-JSON output
    #   _failure="empty"      the model returned valid JSON but with empty lists
    # All three currently degrade gracefully (return empty lists); the
    # _failure label is for telemetry/diagnostics so we can spot drift.
    #
    # APIConnectionError gets per-session retry-with-backoff because we
    # observed (2026-05-14 incident) that long-running cognify processes
    # accumulate stale connections in the SDK's internal httpx pool;
    # marathon `cognify-reextract --all` runs saw the failure rate climb
    # to ~90% Connection error in the last hours of an 18h run. Recycling
    # the client (close+new) on each retry forces a fresh httpx pool and
    # defeats that failure mode. Other exception types are NOT retried —
    # auth errors, 400s, JSONDecodeErrors won't get better from waiting.
    response = None
    api_error: Exception | None = None
    for attempt in range(_API_MAX_RETRIES):
        try:
            response = client.messages.create(
                model=_EXTRACT_MODEL,
                max_tokens=16000,
                system=system_blocks,
                messages=[
                    {
                        "role": "user",
                        "content": f"Extract lessons and decisions from this session:\n\n{content[:200_000]}",
                    },
                    # Prefill the assistant turn with `{` so the model is
                    # locked into continuing as JSON. Per Anthropic's
                    # Messages API: when the final message is role=assistant,
                    # the model's output is a continuation of that string.
                    # Eliminates the most common json_parse failure mode we
                    # observed in the 2026-05-17 brightbot run — Sonnet
                    # abandoning the task and producing conversational
                    # text, code snippets, or input echoes instead of a
                    # JSON object. With the prefill, the model can ONLY
                    # continue as JSON.
                    {"role": "assistant", "content": "{"},
                ],
            )
            break
        except anthropic.APIConnectionError as exc:
            api_error = exc
            if attempt + 1 < _API_MAX_RETRIES:
                backoff = _API_BACKOFF_S[min(attempt, len(_API_BACKOFF_S) - 1)]
                logger.warning(
                    "cognify_extract: APIConnectionError (attempt %d/%d): %s; "
                    "recycling client + retrying in %ds",
                    attempt + 1, _API_MAX_RETRIES, exc, backoff,
                )
                # Drop the stale client + connection pool, then re-create.
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(backoff)
                client = anthropic.Anthropic(**auth_kwargs)
                continue
            # Exhausted retries — fall through to error return below.
        except Exception as exc:  # noqa: BLE001
            # Non-connection errors (auth, 400, model errors, etc.) —
            # don't retry; they won't improve from waiting.
            api_error = exc
            break

    if response is None:
        logger.warning(
            "cognify_extract: LLM API call failed after %d attempts: %s",
            _API_MAX_RETRIES, api_error,
        )
        return {
            "lessons": [],
            "decisions": [],
            "_usage": _empty_usage,
            "_failure": "api",
            "_failure_detail": str(api_error)[:500],
        }

    usage = response.usage
    token_usage = {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    text = response.content[0].text.strip()
    # Re-attach the prefilled `{` from the assistant turn — the API
    # response only contains content AFTER the prefill, not the prefill
    # itself. Without this, every parse would fail on the missing
    # opening brace.
    if not text.startswith("{"):
        text = "{" + text
    result = _parse_json_with_fallbacks(text)
    if result is None:
        # Log a sample of the offending payload so we can spot patterns
        # (e.g. model returning a refusal, hitting max_tokens mid-string,
        # wrapping in extra preamble). Keep it short to avoid flooding logs.
        payload_sample = text[:400] if text else "<empty>"
        logger.warning(
            "cognify_extract: JSON parse failed across all strategies; "
            "model output sample: %r",
            payload_sample,
        )
        return {
            "lessons": [],
            "decisions": [],
            "_usage": token_usage,  # the API call DID succeed; preserve spend
            "_failure": "json_parse",
            "_failure_detail": f"all parse strategies exhausted; sample={payload_sample!r}",
        }
    result["_usage"] = token_usage
    if not result.get("lessons") and not result.get("decisions"):
        result["_failure"] = "empty"
    return result


def _resolve_auth() -> dict[str, Any] | None:
    """Resolve Anthropic credential to SDK kwargs (api_key= or auth_token=).

    Accepts a Console API key OR a subscription OAuth token; the SDK
    routes them through different headers. See cognify._anthropic_auth
    for the resolution order and accepted env var names.
    """
    from cognify._anthropic_auth import resolve_anthropic_auth
    return resolve_anthropic_auth()


def _get_api_key() -> str | None:
    """Deprecated — kept for any external callers. Use _resolve_auth()."""
    import os
    return os.environ.get("ANTHROPIC_API_KEY")


def _parse_json_with_fallbacks(text: str) -> dict | None:
    """Best-effort parse of an LLM JSON response.

    Tries a sequence of strategies — the first that yields a dict with
    'lessons' and/or 'decisions' wins. Returns None if all fail.

    1. Direct `json.loads(text)` — the normal happy path. With assistant
       prefill (`{` as the prefill) this works for ~all real responses.
    2. Strip markdown code fences (```json ... ``` or ``` ... ```)
       anywhere in the text. The 2026-05-17 brightbot retry observed
       Sonnet occasionally pre-amble-ing with "Here's the breakdown:"
       followed by a fenced block.
    3. Find the first balanced `{...}` substring in the text and try
       parsing that. Handles model output that puts JSON in the middle
       of explanatory prose.
    """
    # Strategy 1: direct parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown code fences anywhere in the response.
    # Patterns we've observed:
    #   ```json\n{...}\n```
    #   ```\n{...}\n```
    import re  # noqa: PLC0415

    fence_match = re.search(
        r"```(?:json)?\s*\n(.*?)\n```",
        text,
        re.DOTALL,
    )
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 3: find the largest balanced {…} substring and try parse.
    # Doesn't try to be clever about nested strings; relies on json.loads
    # to fail-fast if the candidate isn't actually JSON.
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return None


def _upsert_memory(
    conn: Any,
    *,
    project_id: Any,
    kind: str,
    title: str,
    content: str,
    session_id: str,
    reextract: bool = False,
    reextract_meta: str | None = None,
) -> tuple[UUID, bool]:
    """Insert an extracted lesson/decision memory row. Returns (id, was_new).

    Idempotency: if a non-archived row with the same (provenance_id,
    kind, title) already exists, returns its id with was_new=False.
    Migration 037's idx_memory_atom_title_unique enforces this at the
    DB level; this check just avoids the round-trip for the common
    case.

    Memory schema constraints:
    - kind must be in: chunk, decision, pattern, issue, session_summary
      Extracted lessons use kind='pattern'; decisions use kind='decision'.
    - tier: 'lesson' for extracted rows.
    - No metadata column exists; reextract_meta is stored in applies_when.

    reextract: if True, the row carries reextracted_from info in applies_when.
    """
    # Map semantic kind to valid DB kind values.
    # Lessons → kind='pattern'; decisions → kind='decision'.
    # Migration 037 made provenance_id load-bearing for atoms too —
    # session_id IS the raw_sessions.id of the source session, and
    # the new idx_memory_atom_title_unique allows N atoms per
    # (session, kind) as long as titles differ.
    db_kind = "pattern" if kind == "lesson" else "decision"

    # applies_when is a JSONB column for extract-related metadata.
    # source_session is kept here for backwards compat with any
    # consumer that reads it directly, but provenance_id is now the
    # canonical link to the source session.
    applies_when: dict = {"source_session": session_id}
    if reextract and reextract_meta:
        applies_when["reextracted_from"] = reextract_meta

    # Idempotency check: same provenance_id + kind + title means we've
    # already extracted this same atom from this session. Skip the
    # round-trip and short-circuit; the DB constraint is the backstop.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND kind = %s "
            "  AND tier = 'lesson' "
            "  AND title = %s "
            "  AND archived_at IS NULL "
            "  AND provenance_id = %s::uuid",
            (project_id, db_kind, title, session_id),
        )
        existing = cur.fetchone()
    if existing:
        return existing[0], False

    # Insert new extract row with provenance_id = the source session.
    cols = [
        "project_id", "kind", "title", "content",
        "tier", "strength", "applies_when", "extraction_version",
        "provenance_id",
    ]
    vals: list = [
        project_id, db_kind, title, content, "lesson", 1.0,
        json.dumps(applies_when), CURRENT_EXTRACTION_VERSION,
        session_id,
    ]

    placeholders = ", ".join(["%s"] * len(cols))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO devbrain.memory ({', '.join(cols)}) "
            f"VALUES ({placeholders}) "
            "RETURNING id",
            vals,
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid, True


def _archive_prior_extracts(
    conn: Any, session_id: str, project_id: Any
) -> int:
    """Archive existing extracted rows for a session before re-extraction.

    Sets archived_at on rows with applies_when->>'source_session' = session_id
    and tier = 'lesson'. These are the rows produced by prior extract runs.
    Never deletes — HIPAA audit trail.
    Returns the count of rows archived.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET archived_at = now() "
            "WHERE project_id = %s "
            "  AND tier = 'lesson' "
            "  AND archived_at IS NULL "
            "  AND (applies_when->>'source_session') = %s",
            (project_id, session_id),
        )
        count = cur.rowcount
    conn.commit()
    logger.info(
        "cognify_extract: archived %d prior extracts for session %s",
        count,
        session_id,
    )
    return count

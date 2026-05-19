"""cognify_fanout — classify a session's per-project relevance + focused summaries.

Phase 8 PR 1 (foundation). This module exposes a single pure helper:

    classify_session(session_id, conn, *, model=...) -> ClassificationResult

It reads the raw_session, renders the project taxonomy, makes one LLM
call, parses the response, and returns a structured result. **It does
NOT write to the DB.** PR 2 will add `run_fanout()` that calls this
helper and inserts the per-project session_summary rows + derived_from
edges.

Spec reference: docs/plans/2026-05-11-phase-8-cross-project-fan-out-design.md
§12 (the 2026-05-19 addendum) for the locked thresholds, prompt shape,
and output schema.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from cognify.fanout_prompt import (
    SESSION_RELEVANCE_THRESHOLD,
    SUMMARY_MAX_CHARS,
    SUMMARY_MIN_CHARS,
    WITHIN_SECTION_THRESHOLD,
    build_system_prompt,
    build_user_message,
    render_taxonomy,
    validate_output,
)

logger = logging.getLogger(__name__)

# Default model. Mirrors the cognify-bulk default. Switch to
# claude-opus-4-7 via the model= kwarg for stuck-case retries.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Max output tokens. 4K is plenty for the JSON shape: even with 6
# sections + 5 per_project entries × 800 chars summary, the response
# stays under ~3500 tokens.
MAX_OUTPUT_TOKENS = 4_000


@dataclass(frozen=True)
class ProjectClassification:
    """One per-project entry from a classifier response.

    Only emitted when ``session_relevance >= SESSION_RELEVANCE_THRESHOLD``
    (already filtered by ``validate_output``).
    """

    project_slug: str
    session_relevance: float
    section_count: int
    focused_summary: str


@dataclass
class ClassificationResult:
    """Output of classify_session().

    ``failure`` is None on success; otherwise one of:
      - "no_session"    — the session_id didn't resolve
      - "no_content"    — session had no chunks/content to classify
      - "no_taxonomy"   — projects table empty / no active projects
      - "no_auth"       — no Anthropic credential configured (test env)
      - "api"           — API call itself errored (network/auth/429)
      - "json_parse"    — model returned non-JSON
      - "empty"         — valid JSON but no project entries cleared threshold
    """

    session_id: str
    per_project: list[ProjectClassification] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    })
    failure: str | None = None
    failure_detail: str | None = None


def _load_active_taxonomy(conn: Any) -> list[dict[str, Any]]:
    """Fetch active projects for the classifier's taxonomy block.

    Excludes archived projects (status filter) and the orphan/home
    catch-alls — those exist for canonical-assignment fallback, not
    as classifier targets. A session that's purely "home-mike" content
    legitimately shouldn't fan out *into* home-mike — it'd be a
    self-reference.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT slug, name, description
            FROM devbrain.projects
            WHERE (status IS NULL OR status = 'active')
              AND slug NOT LIKE 'home-%'
            ORDER BY slug
            """,
        )
        return [
            {"slug": r[0], "name": r[1], "description": r[2]}
            for r in cur.fetchall()
        ]


def _load_session_content(conn: Any, session_id: str) -> str | None:
    """Assemble the classifier's input text from a session's chunks.

    Returns None if the session_id doesn't exist; "" if it exists but
    has no chunks (the classifier won't have anything to work with).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.raw_sessions WHERE id = %s",
            (session_id,),
        )
        if cur.fetchone() is None:
            return None

        # chunks has no chunk_index column today; ordering by
        # (source_line_start, created_at) approximates the source order
        # well enough for the classifier. Falls back gracefully when
        # source_line_start is NULL (codex/openclaw adapters).
        cur.execute(
            """
            SELECT content
            FROM devbrain.chunks
            WHERE source_id = %s
            ORDER BY source_line_start NULLS LAST, created_at
            """,
            (session_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return ""
    return "\n\n".join(r[0] for r in rows if r[0])


def classify_session(
    session_id: str | UUID,
    conn: Any,
    *,
    model: str = DEFAULT_MODEL,
    max_retries: int = 3,
) -> ClassificationResult:
    """Run one classifier LLM call for a session. Returns parsed results.

    Pure function — does NOT write to the DB. Caller (PR 2's
    ``run_fanout``) is responsible for inserting the resulting memory
    rows + ``derived_from`` edges + spend log entry.

    Args:
        session_id: raw_sessions.id (UUID or string form).
        conn: psycopg2 connection.
        model: Anthropic model string. Sonnet for the bulk run; Opus
               retry on json_parse / empty cases.
        max_retries: connection retries on ``APIConnectionError``. Stale
               httpx pool defense — same pattern cognify_extract uses.

    Returns:
        ClassificationResult with ``failure`` set on any failure mode.
        The caller can shard / retry / log accordingly.
    """
    session_id_str = str(session_id)
    result = ClassificationResult(session_id=session_id_str)

    # 1. Load taxonomy + session content.
    taxonomy = _load_active_taxonomy(conn)
    if not taxonomy:
        result.failure = "no_taxonomy"
        result.failure_detail = "no active non-home projects in devbrain.projects"
        return result

    session_text = _load_session_content(conn, session_id_str)
    if session_text is None:
        result.failure = "no_session"
        return result
    if not session_text.strip():
        result.failure = "no_content"
        return result

    valid_slugs = {p["slug"] for p in taxonomy}

    # 2. Resolve Anthropic auth.
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        result.failure = "no_auth"
        result.failure_detail = "anthropic SDK not installed"
        return result

    from cognify.extract import _resolve_auth  # noqa: PLC0415

    auth_kwargs = _resolve_auth()
    if auth_kwargs is None:
        result.failure = "no_auth"
        return result

    # 3. Build prompt + make the call.
    system_text = build_system_prompt(render_taxonomy(taxonomy))
    user_text = build_user_message(session_text)

    # OAuth path needs the Claude Code system-prefix; console API key path
    # returns None and is unaffected. Mirrors cognify_extract's setup.
    from cognify._anthropic_auth import claude_code_system_prefix  # noqa: PLC0415

    system_blocks: list[dict[str, Any]] = []
    oauth_prefix = claude_code_system_prefix()
    if oauth_prefix:
        system_blocks.append({"type": "text", "text": oauth_prefix})
    system_blocks.append(
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
    )

    client = anthropic.Anthropic(**auth_kwargs)
    response = None
    api_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system_blocks,
                messages=[{"role": "user", "content": user_text}],
            )
            break
        except anthropic.APIConnectionError as exc:
            api_error = exc
            # Recycle the client on retry to clear stale httpx pool —
            # same pattern as cognify_extract's _API_MAX_RETRIES loop.
            client = anthropic.Anthropic(**auth_kwargs)
            continue
        except Exception as exc:  # noqa: BLE001
            api_error = exc
            break

    if response is None:
        result.failure = "api"
        result.failure_detail = type(api_error).__name__ + ": " + str(api_error)[:200]
        return result

    # 4. Record usage for spend tracking (caller writes the spend row).
    usage = getattr(response, "usage", None)
    if usage is not None:
        result.usage = {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }

    # 5. Parse + validate.
    text_blocks = [
        b.text for b in (response.content or [])
        if getattr(b, "type", None) == "text"
    ]
    text = "".join(text_blocks).strip()

    from cognify.extract import _parse_json_with_fallbacks  # noqa: PLC0415

    parsed = _parse_json_with_fallbacks(text)
    if parsed is None:
        result.failure = "json_parse"
        result.failure_detail = (
            f"all parse strategies exhausted; sample={text[:400]!r}"
        )
        return result

    validated = validate_output(parsed, valid_slugs)
    result.sections = validated["sections"]
    result.per_project = [
        ProjectClassification(
            project_slug=e["project_slug"],
            session_relevance=e["session_relevance"],
            section_count=e["section_count"],
            focused_summary=_clip_summary(e["focused_summary"]),
        )
        for e in validated["per_project"]
    ]

    if not result.per_project:
        result.failure = "empty"
        result.failure_detail = "no project entries cleared session_relevance threshold"

    return result


def _clip_summary(text: str) -> str:
    """Defensive length clamp — the prompt asks for 200–800 chars but
    models occasionally over/undershoot. We pad NO ONE — if the model
    returns <200 chars we accept it (a terse summary is still better
    than nothing). For >800 we truncate at a sentence boundary if
    possible, else hard-cut.
    """
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    cut = text[:SUMMARY_MAX_CHARS]
    last_period = cut.rfind(". ")
    if last_period >= SUMMARY_MIN_CHARS:
        return cut[: last_period + 1]
    return cut.rstrip() + "…"


# Re-export thresholds at module level for callers/tests that want to
# assert against the locked spec without reaching into fanout_prompt.
__all__ = [
    "SESSION_RELEVANCE_THRESHOLD",
    "WITHIN_SECTION_THRESHOLD",
    "DEFAULT_MODEL",
    "ProjectClassification",
    "ClassificationResult",
    "classify_session",
]

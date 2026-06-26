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

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from cognify.embedding import embed_text
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

# Default classifier model. Routes to the codex CLI backend
# (schema-constrained JSON → no json_parse drops; ChatGPT-sub auth). Override
# with DEVBRAIN_FANOUT_MODEL. On a codex `api` failure (quota/timeout/error)
# we fall back to DEVBRAIN_FANOUT_FALLBACK (default Sonnet) so cross-project
# fan-out never silently drops sessions. Mirrors cognify_extract.
DEFAULT_MODEL = os.environ.get("DEVBRAIN_FANOUT_MODEL", "codex")
_FANOUT_FALLBACK = os.environ.get("DEVBRAIN_FANOUT_FALLBACK", "claude-sonnet-4-6")

# JSON Schema for the classifier's final response. OpenAI strict structured
# output (codex --output-schema) requires every object to set
# additionalProperties:false + list ALL props in `required`, and forbids
# dynamic-key objects. The `sections` field in fanout_prompt's shape uses a
# dynamic-key `project_scores` map (incompatible) and is only advisory anyway
# (run_fanout consumes `per_project` exclusively; validate_output tolerates
# missing sections). So we constrain just the load-bearing `per_project`.
_FANOUT_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["per_project"],
    "properties": {
        "per_project": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "project_slug", "session_relevance",
                    "section_count", "focused_summary",
                ],
                "properties": {
                    "project_slug": {"type": "string"},
                    "session_relevance": {"type": "number"},
                    "section_count": {"type": "integer"},
                    "focused_summary": {"type": "string"},
                },
            },
        },
    },
}


def _codex_classify(
    system_text: str, user_text: str, *, model: str | None = None,
) -> tuple[dict | None, str | None, str | None]:
    """Run the fanout classifier via `codex exec --output-schema`.

    Returns (parsed_dict | None, failure | None, failure_detail). Schema-
    constrained so the output is guaranteed-parseable JSON (no json_parse).
    Mirrors cognify_extract._codex_extract; codex bills the ChatGPT-sub login
    so there's no token spend to record.
    """
    from cognify.extract import _CODEX_BIN, _CODEX_TIMEOUT_S  # noqa: PLC0415

    if not _CODEX_BIN or not os.path.exists(_CODEX_BIN):
        return None, "api", f"codex binary not found at {_CODEX_BIN}"

    prompt = system_text + "\n\n" + user_text
    with tempfile.TemporaryDirectory(prefix="fanout-codex-") as wd:
        schema_path = os.path.join(wd, "schema.json")
        out_path = os.path.join(wd, "out.json")
        with open(schema_path, "w") as fh:
            json.dump(_FANOUT_OUTPUT_SCHEMA, fh)
        cmd = [
            _CODEX_BIN, "exec", "--sandbox", "read-only",
            "--skip-git-repo-check", "--ephemeral", "-C", wd,
            "--output-schema", schema_path, "-o", out_path,
        ]
        if model and model.lower() != "codex":
            cmd += ["-m", model]
        cmd.append(prompt)
        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=_CODEX_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return None, "api", "codex exec timeout"
        if not os.path.exists(out_path):
            return None, "api", (
                f"codex no output (rc={proc.returncode}): "
                f"{(proc.stderr or '')[-300:]}"
            )
        try:
            with open(out_path) as fh:
                return json.load(fh), None, None
        except (json.JSONDecodeError, OSError) as exc:
            return None, "json_parse", str(exc)[:200]


def _anthropic_classify(
    system_text: str, user_text: str, *, model: str, max_retries: int = 3,
) -> tuple[dict | None, str | None, str | None, dict | None]:
    """Classify via the Anthropic SDK. Returns (parsed, failure, detail, usage).

    Best-effort JSON parse (no schema/prefill on the OAuth path) — this is the
    json_parse-prone path codex avoids; kept as the fallback.
    """
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        return None, "no_auth", "anthropic SDK not installed", None

    from cognify._anthropic_auth import claude_code_system_prefix  # noqa: PLC0415
    from cognify.extract import (  # noqa: PLC0415
        _parse_json_with_fallbacks,
        _resolve_auth,
    )

    auth_kwargs = _resolve_auth()
    if auth_kwargs is None:
        return None, "no_auth", None, None

    system_blocks: list[dict[str, Any]] = []
    oauth_prefix = claude_code_system_prefix()
    if oauth_prefix:
        system_blocks.append({"type": "text", "text": oauth_prefix})
    system_blocks.append(
        {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
    )

    client = anthropic.Anthropic(**auth_kwargs)
    response = None
    api_error: Exception | None = None
    for _ in range(max_retries):
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
            client = anthropic.Anthropic(**auth_kwargs)  # recycle stale pool
            continue
        except Exception as exc:  # noqa: BLE001
            api_error = exc
            break

    if response is None:
        return None, "api", type(api_error).__name__ + ": " + str(api_error)[:200], None

    usage = getattr(response, "usage", None)
    usage_d = None
    if usage is not None:
        usage_d = {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }

    text = "".join(
        b.text for b in (response.content or [])
        if getattr(b, "type", None) == "text"
    ).strip()
    parsed = _parse_json_with_fallbacks(text)
    if parsed is None:
        return None, "json_parse", f"all parse strategies exhausted; sample={text[:400]!r}", usage_d
    return parsed, None, None, usage_d

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

    # 2. Build prompt.
    system_text = build_system_prompt(render_taxonomy(taxonomy))
    user_text = build_user_message(session_text)

    # 3. Classify. Prefer codex (schema-constrained JSON → no json_parse
    #    drops); fall back to Anthropic on a codex `api` failure
    #    (quota/timeout/error) so fan-out never silently drops a session.
    from cognify.extract import _routes_to_codex  # noqa: PLC0415

    parsed: dict | None = None
    effective_model = model
    if _routes_to_codex(effective_model):
        parsed, c_fail, c_detail = _codex_classify(
            system_text, user_text, model=effective_model,
        )
        # codex bills the ChatGPT-sub login — no token spend to record.
        result.usage = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }
        if parsed is None:
            if (
                c_fail == "api"
                and _FANOUT_FALLBACK
                and _FANOUT_FALLBACK.lower() not in ("none", "")
                and not _routes_to_codex(_FANOUT_FALLBACK)
            ):
                logger.warning(
                    "cognify_fanout: codex failed (%s); falling back to %s",
                    (c_detail or "")[:160], _FANOUT_FALLBACK,
                )
                effective_model = _FANOUT_FALLBACK
            else:
                result.failure = c_fail
                result.failure_detail = c_detail
                return result

    if parsed is None:
        # Anthropic SDK path — primary for claude-* models, or codex fallback.
        parsed, a_fail, a_detail, a_usage = _anthropic_classify(
            system_text, user_text, model=effective_model, max_retries=max_retries,
        )
        if a_usage is not None:
            result.usage = a_usage
        if parsed is None:
            result.failure = a_fail
            result.failure_detail = a_detail
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


# ─── PR 2 additions: writer + pass registration ─────────────────────────────


@dataclass
class FanoutResult:
    """Aggregate outcome of a run_fanout invocation."""

    sessions_discovered: int = 0
    sessions_processed: int = 0
    sessions_failed: int = 0
    sessions_skipped: int = 0       # idempotency / no-content / no-targets
    rows_emitted: int = 0           # fan-out memory rows written
    llm_calls: int = 0
    failure_counts: dict = field(default_factory=dict)
    dry_run: bool = False


def discover_sessions_needing_fanout(
    conn: Any,
    *,
    project_id: Any = None,
    since=None,
    limit: int | None = None,
) -> list[str]:
    """raw_sessions that lack any active fan-out memory row.

    Discovery rule: a session "needs fan-out" when:
      * it has at least one chunk in `devbrain.chunks` (atomized — there's
        content for the classifier to read),
      * AND no active memory row exists with
        `fanout_source_session_id = rs.id`. The classifier has not yet
        been run, or it ran and produced zero targets (in which case
        we skip rather than re-classify — see §12.7 calibration note).

    Optional filters:
      * `project_id` scopes to sessions whose canonical project_id matches.
      * `since` is a datetime cutoff on `raw_sessions.started_at`.
      * `limit` caps the discover return for shard/dry-run sizing.
    """
    where = ["EXISTS (SELECT 1 FROM devbrain.chunks c WHERE c.source_id = rs.id)"]
    params: list = []

    if project_id is not None:
        where.append("rs.project_id = %s")
        params.append(project_id)
    if since is not None:
        where.append("rs.started_at >= %s")
        params.append(since)

    # Anti-join: skip sessions that already have a fan-out row.
    where.append(
        "NOT EXISTS ("
        " SELECT 1 FROM devbrain.memory m "
        " WHERE m.fanout_source_session_id = rs.id "
        "   AND m.archived_at IS NULL "
        ")"
    )

    sql = (
        "SELECT rs.id::text "
        "FROM devbrain.raw_sessions rs "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY rs.started_at DESC NULLS LAST"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall()]


def apply_shard(sessions: list[str], shard: tuple[int, int] | None) -> list[str]:
    """Stride-slice for parallel runs. shard=(N, M) returns sessions where
    index % M == N. Mirrors cognify-bulk's apply_shard so the operator
    mental model is consistent.
    """
    if shard is None:
        return sessions
    n, m = shard
    if m <= 0 or not (0 <= n < m):
        raise ValueError(f"shard must be (N, M) with 0 <= N < M; got {shard}")
    return [s for i, s in enumerate(sessions) if i % m == n]


def _insert_fanout_row(
    conn: Any,
    *,
    project_id_target: Any,
    source_session_id: str,
    classification: ProjectClassification,
    embedding: list[float] | None,
) -> str | None:
    """Insert one fan-out memory row. Returns the new row's id, or None on
    a DO NOTHING collision (already present — idempotent re-run).

    Inheritance: compliance_profiles are pulled from the TARGET project
    so cross-project rule isolation holds (§12 design decision #4).
    """
    # Title = "<project_slug> session: <date> · <relevance>"
    # Short, scannable in deep_search results. The content holds the
    # focused_summary itself.
    title = f"Cross-project session ({classification.session_relevance:.2f})"

    # Pull target project's compliance_profiles. The projects table stores
    # the per-project allowlist as `compliance_profiles_enabled` (text[]);
    # we propagate that array onto the fan-out row's compliance_profiles
    # column so cross-project rule isolation holds (§12 design decision #4).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compliance_profiles_enabled FROM devbrain.projects "
            "WHERE id = %s",
            (project_id_target,),
        )
        row = cur.fetchone()
    target_profiles = (row[0] if row else None) or []

    # applies_when records the per-fan-out metadata for traceability.
    applies_when = {
        "fanout_source_session": source_session_id,
        "session_relevance": classification.session_relevance,
        "section_count": classification.section_count,
        "source_pass": "cognify_fanout",
    }

    cols = [
        "project_id", "kind", "title", "content", "tier", "strength",
        "applies_when", "fanout_source_session_id", "compliance_profiles",
    ]
    vals: list = [
        project_id_target, "session_summary", title,
        classification.focused_summary, "memory", 1.0,
        _json_dumps(applies_when), source_session_id, target_profiles,
    ]
    placeholders = ["%s"] * len(cols)

    if embedding is not None:
        cols.insert(4, "embedding")
        vals.insert(4, embedding)
        placeholders.insert(4, "%s::vector")

    # ON CONFLICT must repeat the partial index predicate verbatim
    # (Postgres requires matching the inference clause for partial
    # unique indexes).
    sql = (
        "INSERT INTO devbrain.memory (" + ", ".join(cols) + ") "
        "VALUES (" + ", ".join(placeholders) + ") "
        "ON CONFLICT (fanout_source_session_id, project_id) "
        "  WHERE kind = 'session_summary' "
        "    AND tier = 'memory' "
        "    AND archived_at IS NULL "
        "    AND fanout_source_session_id IS NOT NULL "
        "  DO NOTHING "
        "RETURNING id"
    )
    with conn.cursor() as cur:
        cur.execute(sql, vals)
        row = cur.fetchone()
    return row[0] if row else None


def _resolve_project_id_by_slug(conn: Any, slug: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = %s",
            (slug,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _embed_summary(text: str) -> list[float] | None:
    """Compute an embedding for the focused summary via Ollama.

    Returns None if Ollama is unreachable — the fan-out row is still
    written (sans embedding) so search via metadata/title still works;
    a subsequent reembed pass can backfill the vector. Thin wrapper over
    the shared cognify.embedding.embed_text (single source of truth).
    """
    return embed_text(text)


def _json_dumps(obj: dict) -> str:
    import json
    return json.dumps(obj, default=str)


def run_fanout(
    conn: Any,
    *,
    project_id: Any = None,
    since=None,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    shard: tuple[int, int] | None = None,
    max_sessions: int | None = None,
    progress_callback=None,
) -> FanoutResult:
    """Run the Phase 8 cross-project fan-out pass.

    For each discovered raw_session:
      1. Classify via the LLM (one call) into per-project relevance + summary.
      2. For each per_project entry, write a `kind='session_summary'`
         memory row into the target project with `fanout_source_session_id`
         set. Compliance profiles inherited from the target.
      3. Record spend in `cognify_spend_log` (one row per classifier call).

    Idempotency: the (fanout_source_session_id, project_id) partial unique
    index from migration 039 collapses re-emissions into DO NOTHING.

    No `memory_dependencies` edges are emitted today. The
    `fanout_source_session_id` FK column is the linkage; an explicit
    `derived_from` edge would only be valuable for multi-hop graph
    walks, which can be added later without changing this writer.

    Returns FanoutResult with counts. ``dry_run=True`` walks discovery
    + reports counts but skips the LLM call and DB writes.
    """
    from observability.spend import record_spend  # noqa: PLC0415
    from observability.pricing import (  # noqa: PLC0415
        get_pricing, compute_cost_usd, SONNET_4_6,
    )

    result = FanoutResult(dry_run=dry_run)

    sessions = discover_sessions_needing_fanout(
        conn, project_id=project_id, since=since, limit=max_sessions,
    )
    sessions = apply_shard(sessions, shard)
    result.sessions_discovered = len(sessions)

    if dry_run or not sessions:
        return result

    pricing = get_pricing(model) or SONNET_4_6

    for idx, session_id in enumerate(sessions, start=1):
        try:
            classification = classify_session(session_id, conn, model=model)
        except Exception as exc:  # noqa: BLE001
            logger.exception("cognify_fanout: classify_session raised on %s", session_id)
            result.sessions_failed += 1
            result.failure_counts["exception"] = result.failure_counts.get("exception", 0) + 1
            if progress_callback:
                progress_callback(idx, len(sessions), {
                    "session_id": session_id, "failure": "exception", "rows": 0,
                })
            continue

        # Spend tracking — record even on per-project=empty results since
        # the LLM call happened. Skip when classifier short-circuited
        # before the API call (no_taxonomy/no_session/no_content/no_auth).
        usage = classification.usage
        if any(usage.get(k, 0) for k in ("input_tokens", "output_tokens")):
            cost = compute_cost_usd(
                pricing,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_tokens", 0),
                cache_write_tokens=usage.get("cache_write_tokens", 0),
            )
            try:
                record_spend(
                    conn,
                    project_id=project_id,
                    pass_name="fanout",
                    model=model,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_read_tokens=usage.get("cache_read_tokens", 0),
                    cache_write_tokens=usage.get("cache_write_tokens", 0),
                    cost_usd=cost,
                )
            except Exception:  # noqa: BLE001
                logger.exception("cognify_fanout: record_spend failed on %s", session_id)

            result.llm_calls += 1

        if classification.failure:
            if classification.failure == "empty":
                # Empty → the classifier ran but no target cleared 0.30.
                # Count as skipped, not failed (no rework needed; the
                # session legitimately doesn't fan out).
                result.sessions_skipped += 1
            else:
                result.sessions_failed += 1
                key = classification.failure
                result.failure_counts[key] = result.failure_counts.get(key, 0) + 1
            if progress_callback:
                progress_callback(idx, len(sessions), {
                    "session_id": session_id,
                    "failure": classification.failure,
                    "rows": 0,
                })
            continue

        # Happy path: write fan-out rows.
        rows_for_session = 0
        for pc in classification.per_project:
            target_pid = _resolve_project_id_by_slug(conn, pc.project_slug)
            if not target_pid:
                # Project was in taxonomy at classification time but
                # disappeared between then and write (rare). Skip rather
                # than crash.
                logger.warning(
                    "cognify_fanout: target project %r vanished mid-run; skip",
                    pc.project_slug,
                )
                continue
            embedding = _embed_summary(pc.focused_summary)
            new_id = _insert_fanout_row(
                conn,
                project_id_target=target_pid,
                source_session_id=session_id,
                classification=pc,
                embedding=embedding,
            )
            if new_id is not None:
                rows_for_session += 1

        conn.commit()
        result.sessions_processed += 1
        result.rows_emitted += rows_for_session

        if progress_callback:
            progress_callback(idx, len(sessions), {
                "session_id": session_id,
                "rows": rows_for_session,
                "failure": None,
            })

    return result


# ─── Pass registration (orchestrator framework) ─────────────────────────────


def _register_fanout_pass() -> None:
    """Late-binding registration with the cognify orchestrator.

    Imported lazily by orchestrator._ensure_registry() so the import
    graph stays acyclic.
    """
    from cognify.orchestrator import CognifyPass, PassResult, register_pass

    @register_pass
    class FanoutPass(CognifyPass):
        pass_name = "fanout"

        def run(
            self, conn, project_id, *, dry_run: bool = False,
        ) -> "PassResult":
            res = run_fanout(conn, project_id=project_id, dry_run=dry_run)
            return PassResult(
                rows_processed=res.rows_emitted,
                llm_calls=res.llm_calls,
                metadata={
                    "pass": "fanout",
                    "sessions_discovered": res.sessions_discovered,
                    "sessions_processed": res.sessions_processed,
                    "sessions_failed": res.sessions_failed,
                    "sessions_skipped": res.sessions_skipped,
                    "failure_counts": res.failure_counts,
                },
                dry_run=dry_run,
            )

    return FanoutPass


_register_fanout_pass()


# Re-export thresholds at module level for callers/tests that want to
# assert against the locked spec without reaching into fanout_prompt.
__all__ = [
    "SESSION_RELEVANCE_THRESHOLD",
    "WITHIN_SECTION_THRESHOLD",
    "DEFAULT_MODEL",
    "ProjectClassification",
    "ClassificationResult",
    "FanoutResult",
    "classify_session",
    "run_fanout",
    "discover_sessions_needing_fanout",
    "apply_shard",
]

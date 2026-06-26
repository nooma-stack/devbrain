"""Handlers for the new structured-judgment params on end_session().

The calling agent (already in-context with the full session) volunteers
judgment via three optional params. This module persists those decisions
as side-effects.

NO LLM CALL HERE. The LLM call IS the agent that called end_session() —
they're providing the judgment, we're persisting it.

Cross-project isolation
-----------------------
Every handler validates each memory_id belongs to ``session_project_id``
BEFORE applying ANY decisions. If a single memory in the payload lives in
a different project, the entire payload is rejected (ValueError). This is
enforced for the postulate P_end_session_isolation — partial application
would silently leak data across project boundaries.

Idempotency
-----------
``end_session_idempotent_handler`` is the public entry point. Repeat
calls with the same ``(session_id, payload_hash)`` short-circuit to the
prior result. The hash is sha256 of the canonical-JSON payload, so any
edit (added decision, reordered list — note: sort_keys=True normalizes
key order but list order is part of the hash) makes a new row.

Drain trigger
-------------
After applying judgment, the handler drains the cascade queue
(``drain_one_batch(conn, batch_size=200)``). This honors the design
promise that ripple effects propagate from anywhere a session can land —
not just at factory job startup. Drain failure is non-fatal: judgment is
already persisted; the failed drain is logged and recorded in the result
JSONB so an operator can spot it.
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from cognify.embedding import embed_text, to_vector_literal

logger = logging.getLogger(__name__)


# Drain batch size used at end_session time. Larger than the factory job
# startup default (50) because end_session is a natural catch-up moment —
# the user has just stopped, and we want the queue empty for the next
# session start.
END_SESSION_DRAIN_BATCH = 200


class CascadeDecision(BaseModel):
    """A volunteered judgment about a single memory.

    action ∈ {promote, merge, contradict, refine, no_action}:
      - promote   → tier='memory' rows become tier='lesson'
      - contradict → strength halved (cheap proxy until Step 6 lands a
                     proper contradiction agent)
      - refine    → enqueue a self-cascade signal so the Step 6 refinement
                     agent picks it up
      - merge     → no-op in v3.0
      - no_action → no-op
    """

    memory_id: UUID
    action: str
    rationale: str = ""


class NewEdge(BaseModel):
    """A new dependency edge volunteered by the calling agent."""

    from_memory_id: UUID
    to_memory_id: UUID
    edge_type: str  # depends_on | supersedes | contradicts | derived_from


class LessonCandidate(BaseModel):
    """A candidate lesson the calling agent extracted from this session."""

    title: str
    content: str
    applies_when: dict = Field(default_factory=dict)
    compliance_profiles: list[str] = Field(default_factory=list)


def handle_cascade_decisions(
    conn: Any, session_project_id: UUID, decisions: list[CascadeDecision]
) -> None:
    """Apply per-memory actions volunteered by the calling agent.

    Validates every memory_id belongs to the session's project before
    applying ANY decisions (cross-project isolation —
    P_end_session_isolation).
    """
    if not decisions:
        return
    _assert_all_in_project(
        conn, session_project_id, [d.memory_id for d in decisions]
    )
    with conn.cursor() as cur:
        for d in decisions:
            if d.action == "promote":
                cur.execute(
                    "UPDATE devbrain.memory SET tier = 'lesson' "
                    "WHERE id = %s AND tier = 'memory'",
                    (d.memory_id,),
                )
            elif d.action == "contradict":
                # Mark for refinement — actual contradiction handling is
                # Step 6. v3.0: halve the strength so search ranks it
                # lower; future agent can resolve.
                cur.execute(
                    "UPDATE devbrain.memory SET strength = strength * 0.5 "
                    "WHERE id = %s",
                    (d.memory_id,),
                )
            elif d.action == "refine":
                # Step 6 refinement agent picks this up; for now just
                # enqueue a self-cascade signal. ON CONFLICT DO NOTHING
                # against the dedup partial unique index from migration
                # 017 — repeat refines collapse into a single queue row.
                cur.execute(
                    "INSERT INTO devbrain.curator_re_eval_queue "
                    "(memory_id, cascade_source_id, edge_type) "
                    "VALUES (%s, %s, 'applies_when') "
                    "ON CONFLICT (memory_id, cascade_source_id, edge_type) "
                    "WHERE attempt_count < 3 DO NOTHING",
                    (d.memory_id, d.memory_id),  # self-cascade signal
                )
            # merge / no_action: no-op for v3.0
    conn.commit()


def handle_new_relationships(
    conn: Any, session_project_id: UUID, edges: list[NewEdge]
) -> None:
    """Insert into memory_dependencies; ON CONFLICT DO NOTHING.

    Validates ALL ids in the payload belong to ``session_project_id``
    before applying any edge — same wholesale-rejection rule as
    cascade_decisions.
    """
    if not edges:
        return
    all_ids: list[UUID] = []
    for e in edges:
        all_ids.append(e.from_memory_id)
        all_ids.append(e.to_memory_id)
    _assert_all_in_project(conn, session_project_id, all_ids)
    with conn.cursor() as cur:
        for e in edges:
            cur.execute(
                "INSERT INTO devbrain.memory_dependencies "
                "(from_memory_id, to_memory_id, edge_type, created_by) "
                "VALUES (%s, %s, %s, 'end_session') "
                "ON CONFLICT DO NOTHING",
                (e.from_memory_id, e.to_memory_id, e.edge_type),
            )
    conn.commit()


def handle_lesson_candidates(
    conn: Any, session_project_id: UUID, candidates: list[LessonCandidate]
) -> None:
    """Insert new tier='lesson' memory rows.

    Lesson candidates are net-new memory; there's no cross-project
    payload validation step (the session_project_id IS the target). Stored
    as kind='pattern' tier='lesson' with full strength=1.0.
    """
    if not candidates:
        return
    with conn.cursor() as cur:
        for c in candidates:
            # Embed so deep_search can find the lesson (it filters
            # embedding IS NOT NULL). Graceful None if Ollama is down — the
            # row is still written and a reembed pass backfills the vector.
            emb = embed_text(f"{c.title}\n{c.content}" if c.title else c.content)
            if emb is not None:
                cur.execute(
                    "INSERT INTO devbrain.memory "
                    "(project_id, kind, title, content, tier, strength, "
                    " applies_when, embedding) "
                    "VALUES (%s, 'pattern', %s, %s, 'lesson', 1.0, %s::jsonb, %s::vector)",
                    (
                        session_project_id,
                        c.title,
                        c.content,
                        _json.dumps(c.applies_when),
                        to_vector_literal(emb),
                    ),
                )
            else:
                cur.execute(
                    "INSERT INTO devbrain.memory "
                    "(project_id, kind, title, content, tier, strength, "
                    " applies_when) "
                    "VALUES (%s, 'pattern', %s, %s, 'lesson', 1.0, %s::jsonb)",
                    (
                        session_project_id,
                        c.title,
                        c.content,
                        _json.dumps(c.applies_when),
                    ),
                )
    conn.commit()


def end_session_idempotent_handler(
    conn: Any, project_id: UUID, payload: dict
) -> dict:
    """Apply an end_session payload exactly once per (session_id, payload-hash).

    Repeat calls return the prior result without re-applying side effects.
    A different payload under the same session_id is a NEW application
    (different hash → new row → fresh side effects).

    Drains the cascade queue at the end. Drain failure is non-fatal:
    judgment is the user-facing primary work; drain is best-effort
    cleanup. Failed drain rows stay in the queue for the next end_session
    or the next factory job.
    """
    session_id = payload["session_id"]
    # dev_id + cli are attribution columns added in migration 038.
    # Excluded from the payload_hash so existing idempotency keys still match
    # when the MCP server starts passing them on already-logged sessions.
    dev_id = payload.get("dev_id")
    cli = payload.get("cli")
    hashable = {k: v for k, v in payload.items() if k not in ("dev_id", "cli")}
    payload_hash = hashlib.sha256(
        _json.dumps(hashable, sort_keys=True, default=str).encode()
    ).hexdigest()

    # Idempotency check.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT result FROM devbrain.end_session_log "
            "WHERE session_id = %s AND payload_hash = %s",
            (session_id, payload_hash),
        )
        existing = cur.fetchone()
        if existing is not None:
            return existing[0]

    # Apply once. Each handler validates cross-project isolation
    # independently — a ValueError raised here aborts the call before
    # logging anything to end_session_log, so the next retry can re-apply
    # against a corrected payload.
    handle_cascade_decisions(
        conn,
        project_id,
        [CascadeDecision(**d) for d in payload.get("cascade_decisions", [])],
    )
    handle_new_relationships(
        conn,
        project_id,
        [NewEdge(**e) for e in payload.get("new_relationships", [])],
    )
    handle_lesson_candidates(
        conn,
        project_id,
        [
            LessonCandidate(**c)
            for c in payload.get("lesson_candidates", [])
        ],
    )

    result: dict = {"status": "applied"}

    # Drain the cascade queue. Best-effort: a drain failure is logged but
    # does NOT roll back the judgment we just persisted.
    try:
        # Local import to avoid a top-level circular: worker imports from
        # curator.strength, end_session is a curator sibling.
        from curator.worker import drain_one_batch

        drained = drain_one_batch(
            conn, batch_size=END_SESSION_DRAIN_BATCH
        )
        result["cascades_drained"] = drained
    except Exception as exc:  # noqa: BLE001
        logger.exception("end_session drain failed")
        # Restore conn to a clean state — drain_one_batch may have left
        # the transaction aborted. Subsequent INSERT into end_session_log
        # needs a clean cursor.
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        result["cascades_drained"] = 0
        result["drain_error"] = str(exc)[:500]

    # Log the application. INSERT is the natural idempotency seal —
    # subsequent (session_id, payload_hash) hits short-circuit at the
    # SELECT above.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.end_session_log "
            "(session_id, payload_hash, project_id, result, dev_id, cli) "
            "VALUES (%s, %s, %s, %s::jsonb, %s, %s) "
            "ON CONFLICT (session_id, payload_hash) DO NOTHING",
            (
                session_id, payload_hash, project_id, _json.dumps(result),
                dev_id, cli,
            ),
        )
    conn.commit()
    return result


def _assert_all_in_project(conn, project_id, memory_ids):
    if not memory_ids:
        return
    unique_ids = list({mid for mid in memory_ids})
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE id = ANY(%s) AND project_id = %s",
            (unique_ids, project_id),
        )
        count = cur.fetchone()[0]
    if count != len(unique_ids):
        raise ValueError(
            "end_session payload references memories outside the "
            "session's project"
        )

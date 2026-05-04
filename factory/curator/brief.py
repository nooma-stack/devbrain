"""Curator brief generator.

Synchronous; called from FactoryDB.transition() at the QUEUED -> PLANNING
edge. Pure filtering + ranking — no LLM in v3.0. Could become LLM-driven
later without breaking the CuratorBrief v1.0 contract.

Profile filtering depends on the compliance_profiles columns shipped in
Step 7. Until then, all tier='rule' rows are loaded (no profile filter)
— the SELECT is wrapped in try/except + rollback so the same code keeps
working before and after the column ships.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from curator.types import CascadeNote, CuratorBrief, MemoryRef

logger = logging.getLogger(__name__)

LESSON_TOP_N = 20
DECISION_TOP_N = 30
CASCADE_NOTE_LIMIT = 20
CASCADE_SIGNAL_WINDOW = "24 hours"


def generate_brief(
    conn: Any, job_id: UUID, project_id: UUID, spec: str
) -> CuratorBrief:
    """Generate a CuratorBrief and persist to factory_jobs.curator_brief.

    The conn must already be in a clean state (no aborted transaction).
    On success the brief is committed via _persist_to_job. On any
    SELECT-level failure caused by missing Step 7 columns, the function
    rolls back the failed query and falls through to a no-filter SELECT
    so the rest of the brief still builds.
    """
    profiles = _load_enabled_profiles(conn, project_id)
    rules = _load_rules(conn, project_id, profiles)
    lessons = _load_lessons(conn, project_id, top_n=LESSON_TOP_N)
    decisions = _load_decisions_matching(conn, project_id, spec or "")
    cascades = _load_recent_cascades(conn, project_id)

    brief = CuratorBrief(
        version="1.0",
        job_id=job_id,
        project_id=project_id,
        rules=rules,
        lessons=lessons,
        relevant_decisions=decisions,
        recent_cascade_signals=cascades,
        generated_at=datetime.now(timezone.utc),
    )

    _persist_to_job(conn, job_id, brief)
    return brief


def _load_enabled_profiles(conn, project_id) -> list[str]:
    """Step 7 column. Returns [] if column doesn't exist yet."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT compliance_profiles_enabled FROM devbrain.projects "
                "WHERE id = %s",
                (project_id,),
            )
            row = cur.fetchone()
            return list(row[0] or []) if row and row[0] else []
        except Exception:
            conn.rollback()
            return []


def _load_rules(conn, project_id, profiles) -> list[MemoryRef]:
    """Filter by profile intersection if Step 7 column exists; else all rules."""
    base = (
        "SELECT id, kind, title, content, tier, strength, last_cascade_at "
        "FROM devbrain.memory "
        "WHERE project_id = %s AND tier = 'rule' AND archived_at IS NULL"
    )
    with conn.cursor() as cur:
        if profiles:
            try:
                cur.execute(
                    base + " AND compliance_profiles && %s "
                    "ORDER BY strength DESC",
                    (project_id, profiles),
                )
                return [_to_ref(row) for row in cur.fetchall()]
            except Exception:
                conn.rollback()
        cur.execute(base + " ORDER BY strength DESC", (project_id,))
        return [_to_ref(row) for row in cur.fetchall()]


def _load_lessons(conn, project_id, top_n) -> list[MemoryRef]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, title, content, tier, strength, last_cascade_at "
            "FROM devbrain.memory "
            "WHERE project_id = %s AND tier = 'lesson' AND archived_at IS NULL "
            "ORDER BY strength DESC LIMIT %s",
            (project_id, top_n),
        )
        return [_to_ref(row) for row in cur.fetchall()]


def _load_decisions_matching(conn, project_id, spec) -> list[MemoryRef]:
    """Naive matcher v0.1 — substring match against spec text.

    TODO Phase 3.x: smarter matcher (semantic similarity, structured
    applies_when match). Don't change the function signature.
    """
    keywords = [w for w in spec.split() if len(w) > 3][:10]
    with conn.cursor() as cur:
        if keywords:
            patterns = [f"%{w}%" for w in keywords]
            cur.execute(
                "SELECT id, kind, title, content, tier, strength, "
                "       last_cascade_at "
                "FROM devbrain.memory "
                "WHERE project_id = %s AND tier = 'memory' "
                "  AND archived_at IS NULL "
                "  AND content ILIKE ANY(%s) "
                "ORDER BY strength DESC LIMIT %s",
                (project_id, patterns, DECISION_TOP_N),
            )
        else:
            cur.execute(
                "SELECT id, kind, title, content, tier, strength, "
                "       last_cascade_at "
                "FROM devbrain.memory "
                "WHERE project_id = %s AND tier = 'memory' "
                "  AND archived_at IS NULL "
                "ORDER BY strength DESC LIMIT %s",
                (project_id, DECISION_TOP_N),
            )
        return [_to_ref(row) for row in cur.fetchall()]


def _load_recent_cascades(conn, project_id) -> list[CascadeNote]:
    """Surface memory rows whose last_cascade_at falls inside the recent
    window. The cascade source / edge_type are placeholders — the queue
    is drained synchronously so by the time the brief generates the
    triplet is gone. Tracing the original source would require a ledger
    join (TODO Phase 3.x — keeps the brief signature stable).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, last_cascade_at
            FROM devbrain.memory
            WHERE project_id = %s
              AND last_cascade_at >= NOW() - INTERVAL '{CASCADE_SIGNAL_WINDOW}'
              AND archived_at IS NULL
            ORDER BY last_cascade_at DESC
            LIMIT {CASCADE_NOTE_LIMIT}
            """,
            (project_id,),
        )
        notes: list[CascadeNote] = []
        for memory_id, occurred_at in cur.fetchall():
            notes.append(
                CascadeNote(
                    affected_memory_id=memory_id,
                    cascade_source_id=memory_id,  # placeholder — see docstring
                    edge_type="supersedes",       # placeholder — see docstring
                    occurred_at=occurred_at,
                    summary=f"Memory {memory_id} re-evaluated by cascade",
                )
            )
        return notes


def _to_ref(row) -> MemoryRef:
    mid, kind, title, content, tier, strength, last_cascade_at = row
    return MemoryRef(
        id=mid,
        kind=kind,
        title=title,
        content_excerpt=(content or "")[:500],
        tier=tier,
        strength=strength,
        last_cascade_at=last_cascade_at,
    )


def _persist_to_job(conn, job_id, brief: CuratorBrief) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs "
            "SET curator_brief = %s::jsonb "
            "WHERE id = %s",
            (brief.model_dump_json(), job_id),
        )
    conn.commit()

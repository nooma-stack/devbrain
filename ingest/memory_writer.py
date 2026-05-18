"""Adapter helper for dual-writing into devbrain.memory (P2.b).

Reads still go to the legacy tables (chunks/decisions/patterns/issues).
Writes go to BOTH the legacy table and devbrain.memory; the unified
table is best-effort — a memory failure must NOT poison the surrounding
transaction or roll back the legacy write that is the current source
of truth.

Idempotency depends on `kind`. Migration 037 split the historical
broad-atom index into two narrower ones:

  * `idx_memory_session_summary_unique` on (provenance_id, kind)
    WHERE kind='session_summary' — one summary row per session.
  * `idx_memory_atom_title_unique` on (provenance_id, kind, title)
    WHERE kind IN ('pattern','decision','lesson','issue') — many
    atoms per (session, kind) allowed, identical re-extractions fold.
  * Chunks (kind='chunk') have no unique constraint — many per
    session by design. pipeline.py:_process_session calls
    delete_chunks_for_session() before re-ingesting an updated
    session, so chunk re-runs don't accumulate.

record_memory picks the right ON CONFLICT target based on `kind`.
Two concurrent dual-writes for the same atom row collapse via
ON CONFLICT DO NOTHING; chunks just insert as new rows.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SAVEPOINT_NAME = "memory_write_sp"


def record_memory(
    cur,
    *,
    project_id: str,
    kind: str,
    content: str,
    title: str | None = None,
    embedding_sql: str | None = None,
    provenance_id: str | None = None,
    applies_when: dict | None = None,
) -> None:
    """Insert a row into devbrain.memory inside the caller's transaction.

    The caller owns the connection / transaction. We wrap the INSERT in
    a SAVEPOINT/ROLLBACK TO SAVEPOINT so that a failure here (e.g. a
    new CHECK violation, FK miss, or pgvector dimension error) leaves
    the caller's transaction healthy: their subsequent legacy commit
    will succeed.

    Without the savepoint, psycopg2 puts the connection into
    InFailedSqlTransaction on any error and the caller's
    `conn.commit()` silently rolls back the legacy INSERT too — which
    breaks the spec contract that "legacy is the source of truth, the
    memory dual-write is best-effort."

    Args:
        cur: an open psycopg2 cursor on the caller's transaction.
        project_id: required (memory.project_id is NOT NULL — the legacy
            tables allow nulls; callers must skip the dual-write when the
            legacy row has no project).
        kind: one of 'chunk', 'decision', 'pattern', 'issue',
            'session_summary' (CHECK enforced at the DB).
        content: required text (memory.content is NOT NULL).
        title: optional human-friendly title.
        embedding_sql: optional pgvector literal already formatted as
            '[v1,v2,…]' — caller passes the existing legacy embedding
            verbatim. We never recompute embeddings; the legacy row
            already paid that cost.
        provenance_id: legacy row's UUID. If None, no dedup is enforced
            (the partial unique index has WHERE provenance_id IS NOT
            NULL so two NULL-prov rows can both insert).
        applies_when: optional JSONB tag dict. Callers populate it on
            curated rows so canonical filters (e.g. factory_review
            lessons) match without falling back to content-LIKE
            heuristics.
    """
    # SAVEPOINT itself is inside the try: if the caller's transaction is
    # already InFailedSqlTransaction, even SAVEPOINT raises — and the
    # docstring's best-effort guarantee must hold regardless of caller-
    # side transaction state.
    # Pick the ON CONFLICT shape per kind (migration 037).
    _ATOM_KINDS = {"pattern", "decision", "lesson", "issue"}
    if kind == "session_summary":
        on_conflict = (
            "ON CONFLICT (provenance_id, kind) "
            "WHERE provenance_id IS NOT NULL AND kind = 'session_summary' "
            "DO NOTHING"
        )
    elif kind in _ATOM_KINDS:
        on_conflict = (
            "ON CONFLICT (provenance_id, kind, title) "
            "WHERE provenance_id IS NOT NULL "
            "  AND kind IN ('pattern', 'decision', 'lesson', 'issue') "
            "DO NOTHING"
        )
    else:
        # Chunks (and any future non-deduped kind) just insert.
        on_conflict = ""

    try:
        cur.execute(f"SAVEPOINT {_SAVEPOINT_NAME}")
        cur.execute(
            f"""
            INSERT INTO devbrain.memory
                (project_id, kind, title, content, embedding,
                 provenance_id, applies_when)
            VALUES (%s, %s, %s, %s, %s::vector, %s, %s::jsonb)
            {on_conflict}
            """,
            (
                project_id, kind, title, content, embedding_sql,
                provenance_id,
                json.dumps(applies_when) if applies_when is not None else None,
            ),
        )
        cur.execute(f"RELEASE SAVEPOINT {_SAVEPOINT_NAME}")
    except Exception as exc:
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT_NAME}")
            cur.execute(f"RELEASE SAVEPOINT {_SAVEPOINT_NAME}")
        except Exception:
            # SAVEPOINT was never established (or already gone) — nothing
            # to roll back. Swallow so the helper stays best-effort.
            pass
        logger.warning(
            "devbrain.memory dual-write failed (kind=%s, provenance_id=%s): %s",
            kind, provenance_id, exc,
        )

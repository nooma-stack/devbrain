"""Adapter helper for dual-writing into devbrain.memory (P2.b).

Reads still go to the legacy tables (chunks/decisions/patterns/issues).
Writes go to BOTH the legacy table and devbrain.memory; the unified
table is best-effort — a memory failure must NOT poison the surrounding
transaction or roll back the legacy write that is the current source
of truth.

Idempotency: relies on the partial unique index from migration 011
(idx_memory_provenance_kind_unique on (provenance_id, kind) WHERE
provenance_id IS NOT NULL) so two concurrent dual-writes for the same
legacy row collapse to one memory row via ON CONFLICT DO NOTHING.
"""
from __future__ import annotations

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
    """
    # SAVEPOINT itself is inside the try: if the caller's transaction is
    # already InFailedSqlTransaction, even SAVEPOINT raises — and the
    # docstring's best-effort guarantee must hold regardless of caller-
    # side transaction state.
    try:
        cur.execute(f"SAVEPOINT {_SAVEPOINT_NAME}")
        cur.execute(
            """
            INSERT INTO devbrain.memory
                (project_id, kind, title, content, embedding, provenance_id)
            VALUES (%s, %s, %s, %s, %s::vector, %s)
            ON CONFLICT (provenance_id, kind) WHERE provenance_id IS NOT NULL
            DO NOTHING
            """,
            (project_id, kind, title, content, embedding_sql, provenance_id),
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

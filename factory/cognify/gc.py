"""cognify_gc — archive low-strength orphan memory rows.

GC policy: archive (set archived_at) rows whose strength has decayed below
threshold AND have zero outgoing edges (orphan) AND have been idle for
>= 90 days.

HIPAA + audit trail constraint: GC NEVER deletes rows. It only sets
archived_at. The memory_ledger AFTER trigger (migration 015) records the
tier transition. Full audit history is preserved indefinitely.

Threshold: strength < 0.1 AND last active >= 90 days ago AND no outgoing
memory_dependencies edges.

SQL-only pass: zero LLM cost, runs weekly via launchd.
"""
from __future__ import annotations

import logging
from typing import Any

from cognify.orchestrator import CognifyPass, PassResult, register_pass

logger = logging.getLogger(__name__)

# Strength below which a row is a GC candidate.
GC_STRENGTH_THRESHOLD = 0.1

# Minimum idle time before a row is GC-eligible.
GC_IDLE_INTERVAL = "90 days"


@register_pass
class GCPass(CognifyPass):
    """cognify_gc: archive low-strength orphan memory rows.

    SQL-only. Zero LLM calls. Runs weekly via launchd.
    NEVER deletes rows (HIPAA audit trail). Sets archived_at only.
    """

    pass_name = "gc"

    def run(self, conn: Any, project_id: Any, *, dry_run: bool = False) -> PassResult:
        """Archive eligible low-strength orphan rows.

        If project_id is given, only rows in that project are considered.
        If project_id is None, all projects are swept.
        """
        if dry_run:
            return self._dry_run(conn, project_id)

        rows_archived = _archive_orphans(conn, project_id)
        return PassResult(
            rows_processed=rows_archived,
            llm_calls=0,
            metadata={"pass": "gc", "archived_count": rows_archived},
        )

    def _dry_run(self, conn: Any, project_id: Any) -> PassResult:
        count = _count_orphans(conn, project_id)
        return PassResult(
            rows_processed=0,
            llm_calls=0,
            metadata={
                "pass": "gc",
                "dry_run_would_archive": count,
            },
        )



def _gc_candidate_ids(conn: Any, project_id: Any) -> list:
    """Return IDs of rows eligible for GC archival.

    Eligible = archived_at IS NULL AND strength < threshold
               AND idle >= GC_IDLE_INTERVAL AND no outgoing edges.
    """
    idle_expr = "GREATEST(last_cascade_at, last_hit, created_at)"
    if project_id is not None:
        sql = (
            "SELECT id FROM devbrain.memory "
            "WHERE archived_at IS NULL "
            "  AND strength < %(threshold)s "
            "  AND project_id = %(project_id)s "
            "  AND ("
            "      " + idle_expr + " < NOW() - INTERVAL '" + GC_IDLE_INTERVAL + "' "
            "      OR (" + idle_expr + " IS NULL AND created_at < NOW() - INTERVAL '" + GC_IDLE_INTERVAL + "')"
            "  ) "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM devbrain.memory_dependencies d "
            "      WHERE d.from_memory_id = devbrain.memory.id"
            "  )"
        )
        params = {"threshold": GC_STRENGTH_THRESHOLD, "project_id": project_id}
    else:
        sql = (
            "SELECT id FROM devbrain.memory "
            "WHERE archived_at IS NULL "
            "  AND strength < %(threshold)s "
            "  AND ("
            "      " + idle_expr + " < NOW() - INTERVAL '" + GC_IDLE_INTERVAL + "' "
            "      OR (" + idle_expr + " IS NULL AND created_at < NOW() - INTERVAL '" + GC_IDLE_INTERVAL + "')"
            "  ) "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM devbrain.memory_dependencies d "
            "      WHERE d.from_memory_id = devbrain.memory.id"
            "  )"
        )
        params = {"threshold": GC_STRENGTH_THRESHOLD}

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [row[0] for row in cur.fetchall()]


def _count_orphans(conn: Any, project_id: Any) -> int:
    """Count rows that would be archived without mutating."""
    return len(_gc_candidate_ids(conn, project_id))


def _archive_orphans(conn: Any, project_id: Any) -> int:
    """Set archived_at on eligible rows. Never deletes. Returns rows archived."""
    ids = _gc_candidate_ids(conn, project_id)
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET archived_at = now() "
            "WHERE id = ANY(%(ids)s::uuid[])",
            {"ids": ids},
        )
        count = cur.rowcount
    conn.commit()
    logger.info(
        "cognify_gc: archived %d orphan rows (project=%s)", count, project_id
    )
    return count

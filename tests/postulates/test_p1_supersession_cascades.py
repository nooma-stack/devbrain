"""P1 — Supersession cascades.

POSTULATE
---------
When a memory M is superseded by M', every memory that has a
'depends_on' edge to M is re-queued for curator re-evaluation
within the same transaction.

STATUS
------
xfail(strict=True) until the curator agent (Atlas Step 5 in
docs/plans/2026-04-29-phase-3-discipline-layer.md §7) lands. The
substrate that the test stands on (memory_dependencies edges) is
already shipped in migration 014 — what's missing is the curator's
re-evaluation queue.

Strict mode means: the day the curator queue does start surfacing
dependents, this test FLIPS GREEN and CI fails (XPASS). That forces
us back here to remove the xfail marker and own the postulate
properly. Without strict=True the test would silently keep "passing
by failing" forever.
"""
from __future__ import annotations

import pytest


@pytest.mark.xfail(
    strict=True,
    reason="Curator cascade re-eval queue lands in Atlas Step 5; "
    "see docs/plans/2026-04-29-phase-3-discipline-layer.md §4.",
)
def test_supersession_queues_dependent_for_reeval(
    conn, project_factory, memory_factory
):
    project = project_factory("p1")
    m_old = memory_factory(
        project["id"], kind="pattern", content="use aiopg for async pg"
    )
    m_dep = memory_factory(
        project["id"], kind="issue", content="aiopg connection pool deadlock fix"
    )

    # m_dep depends_on m_old
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (m_dep["id"], m_old["id"]),
        )
    conn.commit()

    # Supersede m_old with a new memory and record the supersedes edge.
    m_new = memory_factory(
        project["id"], kind="pattern", content="use asyncpg for async pg"
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = now() WHERE id = %s",
            (m_old["id"],),
        )
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'supersedes', 'postulate-test')",
            (m_new["id"], m_old["id"]),
        )
    conn.commit()

    # The curator re-eval queue does not exist yet. When Step 5 lands
    # this query needs to be replaced with the real reader.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id FROM devbrain.curator_reeval_queue "
            "WHERE project_id = %s",
            (project["id"],),
        )
        queued = [r[0] for r in cur.fetchall()]

    assert m_dep["id"] in queued

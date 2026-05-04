"""P1 — Supersession cascades.

POSTULATE
---------
When a memory M is superseded by M', every memory that has a
'depends_on' edge to M is re-queued for curator re-evaluation
within the same transaction.

STATUS
------
xfail(strict=True) until **Phase 5e** ships the MCP `store()`
cascade-detection-and-enqueue path. Atlas Step 5d ships the brief
generator (P2 flips green) and the cascade worker drainer + queue
substrate, but the enqueue side — converting a `supersedes` edge
write into rows in `devbrain.curator_re_eval_queue` — is the
explicit responsibility of the MCP server's `store()` tool per the
locked design (docs/plans/2026-05-04-step-5-curator-design.md §3.1
Pathway 1) and Phase 5e of the implementation plan
(2026-05-04-step-5-curator-implementation.md §5e-NEW-1).

Strict mode means: when Phase 5e lands and supersession edges start
populating the queue automatically, this test FLIPS GREEN and CI
fails (XPASS). That forces us back here to remove the marker and
own the postulate.
"""
from __future__ import annotations

import pytest


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Cascade enqueue path lives in MCP store() — ships in Phase 5e "
        "(see docs/plans/2026-05-04-step-5-curator-implementation.md §5e-NEW-1). "
        "5d shipped the brief + worker drainer; the enqueue side is gated "
        "behind 5e."
    ),
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

    # The Phase 5e enqueue path should populate the queue here.
    # Until 5e ships this query returns [] and the assertion fails
    # (which is what the xfail marker captures).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s AND edge_type = 'supersedes'",
            (m_old["id"],),
        )
        queued = [r[0] for r in cur.fetchall()]

    assert m_dep["id"] in queued

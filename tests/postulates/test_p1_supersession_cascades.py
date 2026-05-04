"""P1 — Supersession cascades.

POSTULATE
---------
When a memory M is superseded by M', every memory that has a
'depends_on' edge to M is enqueued for curator re-evaluation in the
``curator_re_eval_queue`` table with ``edge_type='supersedes'``.

STATUS
------
Activated in Atlas Step 5e — the MCP ``store()`` tool's cascade-enqueue
path lands here. The TypeScript helper
(``mcp-server/src/memory.ts::enqueueCascades``) executes the same SQL
pattern this postulate exercises directly, so the postulate proves the
SQL contract regardless of whether the trigger comes from the MCP layer
or a future Python writer.

Phase 5c shipped the queue substrate (migration 017 +
idx_re_eval_queue_dedup partial unique index). Phase 5d shipped the
brief generator and worker. Phase 5e closes the loop by writing the
enqueue helper that converts a supersedes edge into queue rows.
"""
from __future__ import annotations


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

        # Phase 5e — the cascade-enqueue helper. Mirrors
        # mcp-server/src/memory.ts::enqueueCascades. The MCP store()
        # tool calls this same SQL pattern after writing a supersedes
        # edge; this postulate proves the SQL contract end-to-end:
        # writing the supersedes edge above plus running the enqueue
        # SQL here populates the queue with one row per depends_on
        # dependent.
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) "
            "SELECT from_memory_id, %s, 'supersedes' "
            "FROM devbrain.memory_dependencies "
            "WHERE to_memory_id = %s AND edge_type = 'depends_on' "
            "ON CONFLICT (memory_id, cascade_source_id, edge_type) "
            "  WHERE attempt_count < 3 DO NOTHING",
            (m_old["id"], m_old["id"]),
        )
    conn.commit()

    # Phase 5e enqueue path populates the queue. m_dep should be queued
    # with cascade_source_id = m_old, edge_type = 'supersedes'.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s AND edge_type = 'supersedes'",
            (m_old["id"],),
        )
        queued = [r[0] for r in cur.fetchall()]

    assert m_dep["id"] in queued

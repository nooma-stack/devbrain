"""P_cognify_gc_archive_only: cognify_gc sets archived_at; never DELETEs."""
from __future__ import annotations

import pytest

from cognify.gc import GCPass


def _setup_gc_candidate(conn, project_id, memory_id):
    """Make a memory row a GC candidate: low strength + old + orphan."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET strength = 0.05, "
            "    last_cascade_at = NOW() - INTERVAL '100 days', "
            "    last_hit = NOW() - INTERVAL '100 days', "
            "    created_at = NOW() - INTERVAL '100 days' "
            "WHERE id = %s",
            (memory_id,),
        )
    conn.commit()


@pytest.mark.db
def test_p_cognify_gc_archive_only(conn, project_factory, memory_factory):
    """GC archives rows (sets archived_at) but does not delete them.

    The row count before and after GC must be identical.
    """
    project = project_factory("p_gc_archonly")
    m = memory_factory(project["id"])
    _setup_gc_candidate(conn, project["id"], m["id"])

    # Row count before GC
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory WHERE id = %s", (m["id"],)
        )
        before = cur.fetchone()[0]

    pass_ = GCPass()
    result = pass_.run(conn, project["id"])

    # Row count after GC — must still be 1 (never deleted)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory WHERE id = %s", (m["id"],)
        )
        after = cur.fetchone()[0]

    # Assertions
    assert before == 1
    assert after == 1, "GC deleted a row — violates HIPAA archive-only constraint"
    assert result.rows_processed >= 1

    # Row should be archived
    with conn.cursor() as cur:
        cur.execute(
            "SELECT archived_at FROM devbrain.memory WHERE id = %s", (m["id"],)
        )
        archived_at = cur.fetchone()[0]
    assert archived_at is not None, "GC did not set archived_at"

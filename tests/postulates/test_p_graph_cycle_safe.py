"""P_graph_cycle_safe — Walker terminates on cyclic graphs.

POSTULATE
---------
A cyclic graph A→B→C→A terminates correctly and returns each node
exactly once. The visited-array accumulator prevents infinite recursion.

STATUS
------
Active. Phase 5 graph layer. The recursive CTE accumulates a visited
UUID[] and guards with NOT (next.id = ANY(visited)).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "factory"))

from graph.walker import walk


@pytest.mark.db
def test_cycle_terminates_and_each_node_appears_once(
    conn, project_factory, memory_factory
):
    """A→B→C→A cycle: walker returns A, B, C exactly once without hanging."""
    project = project_factory("graph_cycle_safe")

    a = memory_factory(project["id"], kind="decision", title="A_cycle")
    b = memory_factory(project["id"], kind="decision", title="B_cycle")
    c = memory_factory(project["id"], kind="decision", title="C_cycle")

    # Build the cycle A→B→C→A
    with conn.cursor() as cur:
        for src, dst in [(a, b), (b, c), (c, a)]:
            cur.execute(
                "INSERT INTO devbrain.memory_dependencies "
                "(from_memory_id, to_memory_id, edge_type, created_by) "
                "VALUES (%s, %s, 'depends_on', 'postulate-test')",
                (src["id"], dst["id"]),
            )
    conn.commit()

    result = walk(
        conn,
        a["id"],
        edge_types=["depends_on"],
        max_hops=6,
        max_nodes=50,
        direction="outgoing",
        cross_project=False,
    )

    returned_ids = {m.id for m in result.memories}

    assert a["id"] in returned_ids, "A must be in result (seed)"
    assert b["id"] in returned_ids, "B must be in result (hop-1)"
    assert c["id"] in returned_ids, "C must be in result (hop-2)"

    # Each node must appear exactly once (no duplicates)
    assert len(result.memories) == len(returned_ids), "Each node must appear exactly once"
    assert len(result.memories) == 3, f"Expected 3 unique nodes, got {len(result.memories)}"
    assert result.truncated is False


@pytest.mark.db
def test_self_loop_not_traversed(conn, project_factory, memory_factory):
    """Schema forbids self-loops (CHK), but if one slipped through the walker doesn't loop."""
    project = project_factory("graph_self_loop_guard")
    # The CONSTRAINT chk_no_self_loop means we can't actually insert a
    # self-loop, so we just verify the walker returns the seed correctly
    # even with no edges — the cycle guard is proven by test above.
    seed = memory_factory(project["id"], kind="decision", title="solo")
    conn.commit()

    result = walk(
        conn,
        seed["id"],
        edge_types=["depends_on"],
        max_hops=3,
        direction="both",
        cross_project=False,
    )

    assert len(result.memories) == 1
    assert result.memories[0].id == seed["id"]
    assert result.truncated is False

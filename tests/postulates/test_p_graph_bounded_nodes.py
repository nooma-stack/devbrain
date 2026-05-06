"""P_graph_bounded_nodes — Walker respects max_nodes and sets truncated.

POSTULATE
---------
A graph with more reachable memories than max_nodes returns exactly
max_nodes results and sets truncated=True. The caller is responsible
for re-invoking with higher limits if needed.

STATUS
------
Active. Phase 5 graph layer. The recursive CTE uses LIMIT max_nodes+1
to detect truncation without fetching unbounded result sets.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "factory"))

from graph.walker import walk


@pytest.mark.db
def test_walker_respects_max_nodes_and_flags_truncated(
    conn, project_factory, memory_factory
):
    """When more nodes are reachable than max_nodes, result is capped and truncated=True."""
    project = project_factory("graph_bounded_nodes")

    # Create a star graph: one seed connected to 15 leaves (all hop=1)
    seed = memory_factory(project["id"], kind="decision", title="seed")
    leaves = [memory_factory(project["id"], kind="pattern", title=f"leaf_{i}") for i in range(15)]

    with conn.cursor() as cur:
        for leaf in leaves:
            cur.execute(
                "INSERT INTO devbrain.memory_dependencies "
                "(from_memory_id, to_memory_id, edge_type, created_by) "
                "VALUES (%s, %s, 'depends_on', 'postulate-test')",
                (seed["id"], leaf["id"]),
            )
    conn.commit()

    # Walk with max_nodes=10 — we have 16 reachable (seed + 15 leaves)
    result = walk(
        conn,
        seed["id"],
        edge_types=["depends_on"],
        max_nodes=10,
        max_hops=3,
        direction="outgoing",
        cross_project=False,
    )

    assert len(result.memories) == 10, f"Expected exactly 10 nodes, got {len(result.memories)}"
    assert result.truncated is True, "truncated must be True when max_nodes is hit"


@pytest.mark.db
def test_walker_not_truncated_when_within_limit(conn, project_factory, memory_factory):
    """When total reachable nodes fit within max_nodes, truncated=False."""
    project = project_factory("graph_not_truncated")

    seed = memory_factory(project["id"], kind="decision", title="seed")
    leaf1 = memory_factory(project["id"], kind="pattern", title="leaf1")
    leaf2 = memory_factory(project["id"], kind="pattern", title="leaf2")

    with conn.cursor() as cur:
        for leaf in [leaf1, leaf2]:
            cur.execute(
                "INSERT INTO devbrain.memory_dependencies "
                "(from_memory_id, to_memory_id, edge_type, created_by) "
                "VALUES (%s, %s, 'depends_on', 'postulate-test')",
                (seed["id"], leaf["id"]),
            )
    conn.commit()

    result = walk(
        conn,
        seed["id"],
        edge_types=["depends_on"],
        max_nodes=50,
        max_hops=3,
        direction="outgoing",
        cross_project=False,
    )

    assert len(result.memories) == 3  # seed + 2 leaves
    assert result.truncated is False, "truncated must be False when all nodes fit"

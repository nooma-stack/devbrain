"""P_graph_same_project — Default walk excludes other-project memories.

POSTULATE
---------
With cross_project=False (default), the walker must never surface
memories from a different project, even when an edge crosses a project
boundary (which should not normally exist, but the FK only enforces
endpoint validity, not same-project constraint on the edge).

This is the graph-layer counterpart of P3 (HIPAA isolation).

STATUS
------
Active. Phase 5 graph layer. The recursive CTE carries seed_project_id
and filters `next_m.project_id = w.seed_project_id` in the expansion
step when cross_project=False.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "factory"))

from graph.walker import walk


@pytest.mark.db
def test_cross_project_false_excludes_other_project_nodes(
    conn, project_factory, memory_factory
):
    """Nodes belonging to a different project must not appear when cross_project=False."""
    project_a = project_factory("graph_proj_a")
    project_b = project_factory("graph_proj_b")

    seed = memory_factory(project_a["id"], kind="decision", title="proj_a_seed")
    neighbor_a = memory_factory(project_a["id"], kind="pattern", title="proj_a_neighbor")
    foreign = memory_factory(project_b["id"], kind="decision", title="proj_b_foreign")

    with conn.cursor() as cur:
        # Same-project edge: seed → neighbor_a (should be followed)
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (seed["id"], neighbor_a["id"]),
        )
        # Cross-project edge: seed → foreign (should NOT be followed when cross_project=False)
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (seed["id"], foreign["id"]),
        )
    conn.commit()

    result = walk(
        conn,
        seed["id"],
        edge_types=["depends_on"],
        max_hops=3,
        direction="outgoing",
        cross_project=False,  # default — must not cross projects
    )

    returned_ids = {m.id for m in result.memories}

    assert seed["id"] in returned_ids, "Seed must be in result"
    assert neighbor_a["id"] in returned_ids, "Same-project neighbor must be in result"
    assert foreign["id"] not in returned_ids, "Foreign-project node must be excluded"

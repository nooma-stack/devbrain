"""P_graph_cross_project — cross_project=True surfaces other-project memories.

POSTULATE
---------
When the caller explicitly opts in with cross_project=True, the walker
follows edges across project boundaries and surfaces nodes from other
projects. This is the opt-in counterpart of P_graph_same_project.

STATUS
------
Active. Phase 5 graph layer. The recursive CTE's cross-project guard
is lifted when cross_project=True is passed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "factory"))

from graph.walker import walk


@pytest.mark.db
def test_cross_project_true_includes_other_project_nodes(
    conn, project_factory, memory_factory
):
    """With cross_project=True, nodes from other projects must appear in results."""
    project_a = project_factory("graph_cross_a")
    project_b = project_factory("graph_cross_b")

    seed = memory_factory(project_a["id"], kind="decision", title="seed_cross")
    same_proj = memory_factory(project_a["id"], kind="pattern", title="same_proj_neighbor")
    foreign = memory_factory(project_b["id"], kind="decision", title="foreign_neighbor")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (seed["id"], same_proj["id"]),
        )
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
        cross_project=True,  # opt-in to cross-project expansion
    )

    returned_ids = {m.id for m in result.memories}

    assert seed["id"] in returned_ids, "Seed must be in result"
    assert same_proj["id"] in returned_ids, "Same-project node must be in result"
    assert foreign["id"] in returned_ids, "Foreign-project node must be in result with cross_project=True"

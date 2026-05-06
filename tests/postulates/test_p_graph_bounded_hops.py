"""P_graph_bounded_hops — Walker respects max_hops.

POSTULATE
---------
A graph with edges to depth 5 (A→B→C→D→E→F) returns at most
max_hops=3 levels when walked from A. Nodes at depth 4+ must not
appear in the result regardless of how many total nodes exist.

STATUS
------
Active. Phase 5 graph layer — walks the memory_dependencies table
using recursive CTEs with a hops < max_hops guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "factory"))

from graph.walker import walk


@pytest.mark.db
def test_walker_respects_max_hops(conn, project_factory, memory_factory):
    """Nodes at depth > max_hops must not appear in the result."""
    project = project_factory("graph_bounded_hops")

    # Build a linear chain: A→B→C→D→E→F (depth 0..5 from A)
    a = memory_factory(project["id"], kind="decision", title="A")
    b = memory_factory(project["id"], kind="decision", title="B")
    c = memory_factory(project["id"], kind="decision", title="C")
    d = memory_factory(project["id"], kind="decision", title="D")
    e = memory_factory(project["id"], kind="decision", title="E")
    f = memory_factory(project["id"], kind="decision", title="F")

    chain = [(a, b), (b, c), (c, d), (d, e), (e, f)]
    with conn.cursor() as cur:
        for src, dst in chain:
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
        max_hops=3,
        direction="outgoing",
        cross_project=False,
    )

    returned_ids = {m.id for m in result.memories}

    # Seed (hop 0) through hop 3: A, B, C, D — depth 4 is E, depth 5 is F
    assert a["id"] in returned_ids, "Seed must always be in result"
    assert b["id"] in returned_ids, "hop-1 node must be included"
    assert c["id"] in returned_ids, "hop-2 node must be included"
    assert d["id"] in returned_ids, "hop-3 node must be included"

    assert e["id"] not in returned_ids, "hop-4 node must be excluded (beyond max_hops=3)"
    assert f["id"] not in returned_ids, "hop-5 node must be excluded (beyond max_hops=3)"

    # Verify hop values are correct
    hop_map = {m.id: m.hops for m in result.memories}
    assert hop_map[a["id"]] == 0
    assert hop_map[b["id"]] == 1
    assert hop_map[c["id"]] == 2
    assert hop_map[d["id"]] == 3

"""P_graph_archived_excluded — Archived memories are excluded; walker stops at them.

POSTULATE
---------
Archived memories (archived_at IS NOT NULL) must not appear in walker
results. Furthermore, the walker stops at archived node boundaries —
it will not traverse through an archived intermediate node to reach
non-archived nodes beyond it.

STATUS
------
Active. Phase 5 graph layer. The recursive CTE filters
`m.archived_at IS NULL` in both the base case and the expansion step.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "factory"))

from graph.walker import walk


@pytest.mark.db
def test_archived_leaf_excluded(conn, project_factory, memory_factory):
    """An archived leaf node must not appear in walker results."""
    project = project_factory("graph_archived_leaf")

    seed = memory_factory(project["id"], kind="decision", title="seed_arch")
    live = memory_factory(project["id"], kind="pattern", title="live_neighbor")

    # Insert the archived node
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, title, content, archived_at) "
            "VALUES (%s, 'decision', 'archived_leaf', 'archived content', now()) "
            "RETURNING id",
            (project["id"],),
        )
        archived_id = cur.fetchone()[0]

        # Edge from seed to archived leaf
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (seed["id"], archived_id),
        )
        # Edge from seed to live neighbor
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (seed["id"], live["id"]),
        )
    conn.commit()

    result = walk(
        conn,
        seed["id"],
        edge_types=["depends_on"],
        max_hops=3,
        direction="outgoing",
        cross_project=False,
    )

    returned_ids = {m.id for m in result.memories}

    assert seed["id"] in returned_ids, "Seed must be in result"
    assert live["id"] in returned_ids, "Live neighbor must be in result"
    assert archived_id not in returned_ids, "Archived leaf must be excluded"


@pytest.mark.db
def test_archived_intermediate_blocks_traversal(conn, project_factory, memory_factory):
    """Traversal stops at archived nodes — nodes beyond them are also excluded."""
    project = project_factory("graph_archived_intermediate")

    seed = memory_factory(project["id"], kind="decision", title="seed_ai")
    beyond = memory_factory(project["id"], kind="pattern", title="beyond_archived")

    with conn.cursor() as cur:
        # Insert archived intermediate
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, title, content, archived_at) "
            "VALUES (%s, 'decision', 'archived_mid', 'archived content', now()) "
            "RETURNING id",
            (project["id"],),
        )
        archived_mid = cur.fetchone()[0]

        # Chain: seed → archived_mid → beyond
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (seed["id"], archived_mid),
        )
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (archived_mid, beyond["id"]),
        )
    conn.commit()

    result = walk(
        conn,
        seed["id"],
        edge_types=["depends_on"],
        max_hops=6,
        direction="outgoing",
        cross_project=False,
    )

    returned_ids = {m.id for m in result.memories}

    assert seed["id"] in returned_ids, "Seed must be in result"
    assert archived_mid not in returned_ids, "Archived intermediate must be excluded"
    assert beyond["id"] not in returned_ids, "Node beyond archived intermediate must be excluded"

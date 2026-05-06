"""P_graph_edge_filter — edge_types filter restricts traversal.

POSTULATE
---------
When edge_types=['supersedes'] is passed, the walker only traverses
supersedes edges. Nodes reachable only via other edge types (e.g.
depends_on) must not appear in the result.

STATUS
------
Active. Phase 5 graph layer. The recursive CTE filters
`d.edge_type = ANY(%(edge_types)s)` in the expansion step.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "factory"))

from graph.walker import walk


@pytest.mark.db
def test_edge_type_filter_restricts_traversal(conn, project_factory, memory_factory):
    """Only edges of the requested type should be followed."""
    project = project_factory("graph_edge_filter")

    seed = memory_factory(project["id"], kind="decision", title="seed_ef")
    via_supersedes = memory_factory(project["id"], kind="decision", title="via_supersedes")
    via_depends = memory_factory(project["id"], kind="decision", title="via_depends")
    via_derived = memory_factory(project["id"], kind="decision", title="via_derived")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'supersedes', 'postulate-test')",
            (seed["id"], via_supersedes["id"]),
        )
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (seed["id"], via_depends["id"]),
        )
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'derived_from', 'postulate-test')",
            (seed["id"], via_derived["id"]),
        )
    conn.commit()

    result = walk(
        conn,
        seed["id"],
        edge_types=["supersedes"],  # only follow supersedes
        max_hops=3,
        direction="outgoing",
        cross_project=False,
    )

    returned_ids = {m.id for m in result.memories}

    assert seed["id"] in returned_ids, "Seed must be in result"
    assert via_supersedes["id"] in returned_ids, "Node reachable via supersedes must be included"
    assert via_depends["id"] not in returned_ids, "Node reachable only via depends_on must be excluded"
    assert via_derived["id"] not in returned_ids, "Node reachable only via derived_from must be excluded"


@pytest.mark.db
def test_edge_type_filter_multi_type(conn, project_factory, memory_factory):
    """Multiple edge types in the filter: only those types are followed."""
    project = project_factory("graph_edge_filter_multi")

    seed = memory_factory(project["id"], kind="decision", title="seed_efm")
    via_supersedes = memory_factory(project["id"], kind="decision", title="via_sup")
    via_derived = memory_factory(project["id"], kind="decision", title="via_der")
    via_cites = memory_factory(project["id"], kind="decision", title="via_cit")

    with conn.cursor() as cur:
        for dst, etype in [
            (via_supersedes, "supersedes"),
            (via_derived, "derived_from"),
            (via_cites, "cites"),
        ]:
            cur.execute(
                "INSERT INTO devbrain.memory_dependencies "
                "(from_memory_id, to_memory_id, edge_type, created_by) "
                "VALUES (%s, %s, %s, 'postulate-test')",
                (seed["id"], dst["id"], etype),
            )
    conn.commit()

    result = walk(
        conn,
        seed["id"],
        edge_types=["supersedes", "derived_from"],
        max_hops=3,
        direction="outgoing",
        cross_project=False,
    )

    returned_ids = {m.id for m in result.memories}
    assert via_supersedes["id"] in returned_ids
    assert via_derived["id"] in returned_ids
    assert via_cites["id"] not in returned_ids, "cites not in filter, must be excluded"

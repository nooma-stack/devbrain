"""Unit/integration tests for factory.graph.walker.

Tests exercise the walk() function against the real Postgres DB
(gated on DEVBRAIN_DB_PASSWORD being set). They create isolated
projects via project_factory so there is no cross-test contamination.

Coverage targets:
  - Empty graph (seed only, no edges)
  - Single hop outgoing
  - Single hop incoming
  - Both directions
  - Multi-hop chain
  - Cycle termination
  - max_hops cap
  - max_nodes truncation
  - Edge type filter
  - cross_project=False isolation
  - cross_project=True expansion
  - Archived seed returns empty
  - Archived intermediate blocks further traversal
  - Result ordering (hops asc, strength desc)
  - Edge set returned for intra-walk edges only
"""
from __future__ import annotations

import sys
from pathlib import Path
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from graph.walker import (
    GraphWalkResult,
    MemoryRef,
    EdgeRef,
    STRONG_SIGNAL_EDGE_TYPES,
    walk,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def insert_edge(conn, from_id, to_id, edge_type="depends_on"):
    """Insert a memory_dependencies edge and commit."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, %s, 'test_graph_walker') "
            "ON CONFLICT (from_memory_id, to_memory_id, edge_type) DO NOTHING",
            (from_id, to_id, edge_type),
        )
    conn.commit()


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.db
def test_seed_only_no_edges(conn, project_factory, memory_factory):
    """Seed with no edges returns just the seed, truncated=False."""
    project = project_factory("gw_seed_only")
    seed = memory_factory(project["id"], kind="decision", title="solo")
    conn.commit()

    result = walk(conn, seed["id"], edge_types=["depends_on"])

    assert len(result.memories) == 1
    assert result.memories[0].id == seed["id"]
    assert result.memories[0].hops == 0
    assert result.edges == []
    assert result.truncated is False


@pytest.mark.db
def test_single_hop_outgoing(conn, project_factory, memory_factory):
    """Outgoing single hop: A→B; result contains A (hop 0) and B (hop 1)."""
    project = project_factory("gw_single_hop_out")
    a = memory_factory(project["id"], kind="decision", title="A_out")
    b = memory_factory(project["id"], kind="pattern", title="B_out")
    insert_edge(conn, a["id"], b["id"])

    result = walk(conn, a["id"], edge_types=["depends_on"], direction="outgoing")

    ids = {m.id for m in result.memories}
    assert a["id"] in ids
    assert b["id"] in ids
    hop_map = {m.id: m.hops for m in result.memories}
    assert hop_map[a["id"]] == 0
    assert hop_map[b["id"]] == 1


@pytest.mark.db
def test_single_hop_incoming(conn, project_factory, memory_factory):
    """Incoming single hop: A←B; walking from A with direction='incoming' surfaces B."""
    project = project_factory("gw_single_hop_in")
    a = memory_factory(project["id"], kind="decision", title="A_in")
    b = memory_factory(project["id"], kind="pattern", title="B_in")
    # Edge goes FROM b TO a
    insert_edge(conn, b["id"], a["id"])

    result = walk(conn, a["id"], edge_types=["depends_on"], direction="incoming")

    ids = {m.id for m in result.memories}
    assert a["id"] in ids
    assert b["id"] in ids


@pytest.mark.db
def test_direction_outgoing_excludes_incoming_only_nodes(conn, project_factory, memory_factory):
    """direction='outgoing' must not surface nodes reachable only via incoming edges."""
    project = project_factory("gw_dir_out")
    seed = memory_factory(project["id"], kind="decision", title="seed_dir")
    out_node = memory_factory(project["id"], kind="pattern", title="out_node")
    in_only = memory_factory(project["id"], kind="pattern", title="in_only")

    insert_edge(conn, seed["id"], out_node["id"])   # outgoing
    insert_edge(conn, in_only["id"], seed["id"])     # incoming edge to seed

    result = walk(conn, seed["id"], edge_types=["depends_on"], direction="outgoing")
    ids = {m.id for m in result.memories}

    assert out_node["id"] in ids
    assert in_only["id"] not in ids


@pytest.mark.db
def test_direction_both_expands_in_and_out(conn, project_factory, memory_factory):
    """direction='both' surfaces nodes reachable via both incoming and outgoing edges."""
    project = project_factory("gw_dir_both")
    seed = memory_factory(project["id"], kind="decision", title="seed_both")
    out_node = memory_factory(project["id"], kind="pattern", title="out_both")
    in_node = memory_factory(project["id"], kind="pattern", title="in_both")

    insert_edge(conn, seed["id"], out_node["id"])  # outgoing
    insert_edge(conn, in_node["id"], seed["id"])   # incoming

    result = walk(conn, seed["id"], edge_types=["depends_on"], direction="both")
    ids = {m.id for m in result.memories}

    assert out_node["id"] in ids
    assert in_node["id"] in ids


@pytest.mark.db
def test_multi_hop_chain(conn, project_factory, memory_factory):
    """Multi-hop: A→B→C→D with max_hops=3 returns all 4 at correct hops."""
    project = project_factory("gw_multi_hop")
    a = memory_factory(project["id"], kind="decision", title="A_multi")
    b = memory_factory(project["id"], kind="decision", title="B_multi")
    c = memory_factory(project["id"], kind="decision", title="C_multi")
    d = memory_factory(project["id"], kind="decision", title="D_multi")

    for src, dst in [(a, b), (b, c), (c, d)]:
        insert_edge(conn, src["id"], dst["id"])

    result = walk(conn, a["id"], edge_types=["depends_on"], max_hops=3, direction="outgoing")

    hop_map = {m.id: m.hops for m in result.memories}
    assert hop_map[a["id"]] == 0
    assert hop_map[b["id"]] == 1
    assert hop_map[c["id"]] == 2
    assert hop_map[d["id"]] == 3


@pytest.mark.db
def test_max_hops_caps_traversal(conn, project_factory, memory_factory):
    """Nodes beyond max_hops must not appear."""
    project = project_factory("gw_max_hops")
    nodes = [memory_factory(project["id"], kind="decision", title=f"n{i}") for i in range(6)]
    for i in range(5):
        insert_edge(conn, nodes[i]["id"], nodes[i + 1]["id"])

    result = walk(conn, nodes[0]["id"], edge_types=["depends_on"], max_hops=2, direction="outgoing")
    ids = {m.id for m in result.memories}

    assert nodes[0]["id"] in ids  # hop 0
    assert nodes[1]["id"] in ids  # hop 1
    assert nodes[2]["id"] in ids  # hop 2
    assert nodes[3]["id"] not in ids  # hop 3 — excluded
    assert nodes[4]["id"] not in ids  # hop 4 — excluded
    assert nodes[5]["id"] not in ids  # hop 5 — excluded


@pytest.mark.db
def test_max_nodes_truncation(conn, project_factory, memory_factory):
    """Fetching more nodes than max_nodes sets truncated=True and returns max_nodes rows."""
    project = project_factory("gw_truncation")
    seed = memory_factory(project["id"], kind="decision", title="seed_trunc")
    leaves = [
        memory_factory(project["id"], kind="pattern", title=f"leaf_{i}")
        for i in range(20)
    ]
    for leaf in leaves:
        insert_edge(conn, seed["id"], leaf["id"])

    result = walk(
        conn,
        seed["id"],
        edge_types=["depends_on"],
        max_nodes=10,
        direction="outgoing",
    )

    assert len(result.memories) == 10
    assert result.truncated is True


@pytest.mark.db
def test_no_truncation_within_limit(conn, project_factory, memory_factory):
    """When total nodes fit within max_nodes, truncated=False."""
    project = project_factory("gw_no_trunc")
    seed = memory_factory(project["id"], kind="decision", title="seed_nt")
    leaf = memory_factory(project["id"], kind="pattern", title="leaf_nt")
    insert_edge(conn, seed["id"], leaf["id"])

    result = walk(
        conn,
        seed["id"],
        edge_types=["depends_on"],
        max_nodes=50,
        direction="outgoing",
    )

    assert len(result.memories) == 2
    assert result.truncated is False


@pytest.mark.db
def test_cycle_terminates(conn, project_factory, memory_factory):
    """A→B→C→A cycle: walk terminates and returns each node exactly once."""
    project = project_factory("gw_cycle")
    a = memory_factory(project["id"], kind="decision", title="A_cyc")
    b = memory_factory(project["id"], kind="decision", title="B_cyc")
    c = memory_factory(project["id"], kind="decision", title="C_cyc")

    for src, dst in [(a, b), (b, c), (c, a)]:
        insert_edge(conn, src["id"], dst["id"])

    result = walk(
        conn, a["id"], edge_types=["depends_on"], max_hops=10, direction="outgoing"
    )

    ids = list(m.id for m in result.memories)
    assert len(ids) == len(set(ids)), "No duplicates"
    assert len(ids) == 3


@pytest.mark.db
def test_edge_type_filter(conn, project_factory, memory_factory):
    """Only edges of the requested types are followed."""
    project = project_factory("gw_etype")
    seed = memory_factory(project["id"], kind="decision", title="seed_et")
    sup_node = memory_factory(project["id"], kind="decision", title="sup_node")
    dep_node = memory_factory(project["id"], kind="decision", title="dep_node")

    insert_edge(conn, seed["id"], sup_node["id"], "supersedes")
    insert_edge(conn, seed["id"], dep_node["id"], "depends_on")

    result = walk(
        conn, seed["id"], edge_types=["supersedes"], direction="outgoing"
    )
    ids = {m.id for m in result.memories}
    assert sup_node["id"] in ids
    assert dep_node["id"] not in ids


@pytest.mark.db
def test_cross_project_false_default(conn, project_factory, memory_factory):
    """cross_project=False must exclude nodes from other projects."""
    project_a = project_factory("gw_cp_a")
    project_b = project_factory("gw_cp_b")

    seed = memory_factory(project_a["id"], kind="decision", title="seed_cp")
    foreign = memory_factory(project_b["id"], kind="decision", title="foreign_cp")
    insert_edge(conn, seed["id"], foreign["id"])

    result = walk(
        conn, seed["id"], edge_types=["depends_on"], direction="outgoing", cross_project=False
    )
    ids = {m.id for m in result.memories}
    assert foreign["id"] not in ids


@pytest.mark.db
def test_cross_project_true_includes_foreign(conn, project_factory, memory_factory):
    """cross_project=True must include nodes from other projects."""
    project_a = project_factory("gw_cp_true_a")
    project_b = project_factory("gw_cp_true_b")

    seed = memory_factory(project_a["id"], kind="decision", title="seed_cpt")
    foreign = memory_factory(project_b["id"], kind="decision", title="foreign_cpt")
    insert_edge(conn, seed["id"], foreign["id"])

    result = walk(
        conn, seed["id"], edge_types=["depends_on"], direction="outgoing", cross_project=True
    )
    ids = {m.id for m in result.memories}
    assert foreign["id"] in ids


@pytest.mark.db
def test_archived_seed_returns_empty(conn, project_factory):
    """Walking from an archived seed must return an empty result."""
    project = project_factory("gw_arch_seed")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, title, content, archived_at) "
            "VALUES (%s, 'decision', 'archived_seed', 'content', now()) "
            "RETURNING id",
            (project["id"],),
        )
        archived_id = cur.fetchone()[0]
    conn.commit()

    result = walk(conn, archived_id, edge_types=["depends_on"])

    assert result.memories == []
    assert result.edges == []
    assert result.truncated is False


@pytest.mark.db
def test_archived_node_excluded_and_blocks_traversal(conn, project_factory, memory_factory):
    """Archived intermediate nodes are excluded and block further traversal."""
    project = project_factory("gw_arch_mid")
    seed = memory_factory(project["id"], kind="decision", title="seed_am")
    beyond = memory_factory(project["id"], kind="pattern", title="beyond_am")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, title, content, archived_at) "
            "VALUES (%s, 'decision', 'archived_am', 'content', now()) RETURNING id",
            (project["id"],),
        )
        mid_archived = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test_gw') ",
            (seed["id"], mid_archived),
        )
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test_gw') ",
            (mid_archived, beyond["id"]),
        )
    conn.commit()

    result = walk(conn, seed["id"], edge_types=["depends_on"], max_hops=6, direction="outgoing")
    ids = {m.id for m in result.memories}

    assert seed["id"] in ids
    assert mid_archived not in ids
    assert beyond["id"] not in ids


@pytest.mark.db
def test_result_sorted_hops_asc_strength_desc(conn, project_factory, memory_factory):
    """Memories must be sorted: hops ascending, then strength descending within same hop."""
    project = project_factory("gw_sort")
    seed = memory_factory(project["id"], kind="decision", title="seed_sort", strength=0.8)

    # Two hop-1 nodes with different strengths
    high = memory_factory(project["id"], kind="decision", title="high_str", strength=0.9)
    low = memory_factory(project["id"], kind="decision", title="low_str", strength=0.3)
    far = memory_factory(project["id"], kind="decision", title="far_node", strength=0.5)

    insert_edge(conn, seed["id"], high["id"])
    insert_edge(conn, seed["id"], low["id"])
    insert_edge(conn, high["id"], far["id"])

    result = walk(conn, seed["id"], edge_types=["depends_on"], max_hops=3, direction="outgoing")

    # Seed must be first (hop 0)
    assert result.memories[0].id == seed["id"]

    # Among hop-1 nodes, high strength first
    hop1 = [m for m in result.memories if m.hops == 1]
    assert len(hop1) == 2
    assert hop1[0].strength >= hop1[1].strength, "hop-1 nodes should be strength desc"

    # hop-2 node should come last
    assert result.memories[-1].id == far["id"]


@pytest.mark.db
def test_edges_only_within_returned_node_set(conn, project_factory, memory_factory):
    """Edges in the result must only connect nodes in the memories list."""
    project = project_factory("gw_edges")
    a = memory_factory(project["id"], kind="decision", title="A_edges")
    b = memory_factory(project["id"], kind="decision", title="B_edges")
    c = memory_factory(project["id"], kind="decision", title="C_edges")

    insert_edge(conn, a["id"], b["id"])
    insert_edge(conn, b["id"], c["id"])

    # Walk with max_hops=1 — only A and B returned; edge A→C must NOT appear
    result = walk(
        conn, a["id"], edge_types=["depends_on"], max_hops=1, direction="outgoing"
    )

    returned_ids = {m.id for m in result.memories}
    assert c["id"] not in returned_ids  # sanity: C not in nodes

    for edge in result.edges:
        assert edge.from_memory_id in returned_ids
        assert edge.to_memory_id in returned_ids

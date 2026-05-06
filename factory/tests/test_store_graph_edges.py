"""Tests for store() derived_from and refined_by edge support (Phase 5c).

These tests verify that the new edge types can be inserted into
memory_dependencies with the expected idempotency semantics. They
exercise the database layer directly (not the TypeScript MCP server)
since the TypeScript side is covered by the existing store() tests.

The tests mirror the pattern used by test_store_cascade_enqueue.py:
direct DB inserts + assertions, no MCP round-trip required.
"""
from __future__ import annotations

import sys
from pathlib import Path
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── Helper ──────────────────────────────────────────────────────────────────

def insert_dep(conn, from_id, to_id, edge_type, created_by="test_store_graph_edges"):
    """Insert a memory_dependencies edge."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (from_memory_id, to_memory_id, edge_type) DO NOTHING",
            (from_id, to_id, edge_type, created_by),
        )
    conn.commit()


def get_edges(conn, from_id, to_id=None, edge_type=None):
    """Fetch edges matching the given filters."""
    conditions = ["from_memory_id = %s"]
    params = [from_id]
    if to_id is not None:
        conditions.append("to_memory_id = %s")
        params.append(to_id)
    if edge_type is not None:
        conditions.append("edge_type = %s")
        params.append(edge_type)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT from_memory_id, to_memory_id, edge_type, confidence "
            f"FROM devbrain.memory_dependencies "
            f"WHERE {' AND '.join(conditions)}",
            params,
        )
        return cur.fetchall()


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.db
def test_derived_from_edge_insertable(conn, project_factory, memory_factory):
    """derived_from edge type is accepted by the constraint and lands in DB."""
    project = project_factory("store5c_df")
    lesson = memory_factory(project["id"], kind="decision", title="lesson_5c")
    session = memory_factory(project["id"], kind="pattern", title="session_5c")

    insert_dep(conn, lesson["id"], session["id"], "derived_from")

    edges = get_edges(conn, lesson["id"], to_id=session["id"], edge_type="derived_from")
    assert len(edges) == 1, "derived_from edge must land in DB"
    assert edges[0][2] == "derived_from"


@pytest.mark.db
def test_refined_by_edge_insertable(conn, project_factory, memory_factory):
    """refined_by edge type is accepted by the constraint and lands in DB."""
    project = project_factory("store5c_rb")
    orig = memory_factory(project["id"], kind="decision", title="orig_5c")
    refined = memory_factory(project["id"], kind="decision", title="refined_5c")

    insert_dep(conn, orig["id"], refined["id"], "refined_by")

    edges = get_edges(conn, orig["id"], to_id=refined["id"], edge_type="refined_by")
    assert len(edges) == 1, "refined_by edge must land in DB"
    assert edges[0][2] == "refined_by"


@pytest.mark.db
def test_derived_from_idempotent(conn, project_factory, memory_factory):
    """Duplicate derived_from insert is a no-op (ON CONFLICT DO NOTHING)."""
    project = project_factory("store5c_idem_df")
    lesson = memory_factory(project["id"], kind="decision", title="lesson_idem")
    session = memory_factory(project["id"], kind="pattern", title="session_idem")

    insert_dep(conn, lesson["id"], session["id"], "derived_from")
    insert_dep(conn, lesson["id"], session["id"], "derived_from")  # duplicate

    edges = get_edges(conn, lesson["id"], to_id=session["id"], edge_type="derived_from")
    assert len(edges) == 1, "Duplicate insert must be deduplicated"


@pytest.mark.db
def test_refined_by_idempotent(conn, project_factory, memory_factory):
    """Duplicate refined_by insert is a no-op (ON CONFLICT DO NOTHING)."""
    project = project_factory("store5c_idem_rb")
    orig = memory_factory(project["id"], kind="decision", title="orig_idem")
    refined = memory_factory(project["id"], kind="decision", title="refined_idem")

    insert_dep(conn, orig["id"], refined["id"], "refined_by")
    insert_dep(conn, orig["id"], refined["id"], "refined_by")  # duplicate

    edges = get_edges(conn, orig["id"], to_id=refined["id"], edge_type="refined_by")
    assert len(edges) == 1, "Duplicate insert must be deduplicated"


@pytest.mark.db
def test_derived_from_does_not_enqueue_cascade(conn, project_factory, memory_factory):
    """derived_from edges must NOT trigger cascade re-evaluation queue entries.

    Only supersedes edges trigger cascades. derived_from is a knowledge-
    provenance signal, not a dependency-invalidation signal.
    """
    project = project_factory("store5c_no_cascade")
    lesson = memory_factory(project["id"], kind="decision", title="lesson_nc")
    session = memory_factory(project["id"], kind="pattern", title="session_nc")

    insert_dep(conn, lesson["id"], session["id"], "derived_from")

    # No rows should appear in curator_re_eval_queue for this edge
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s",
            (session["id"],),
        )
        count = cur.fetchone()[0]

    assert count == 0, "derived_from edges must not enqueue cascade re-evaluation"


@pytest.mark.db
def test_both_new_edge_types_plus_existing_on_same_memory(
    conn, project_factory, memory_factory
):
    """A single memory can have all 4 outgoing edge types (all Phase 5 types)."""
    project = project_factory("store5c_multi")
    src = memory_factory(project["id"], kind="decision", title="src_multi")
    t1 = memory_factory(project["id"], kind="decision", title="t1_depends")
    t2 = memory_factory(project["id"], kind="decision", title="t2_supersedes")
    t3 = memory_factory(project["id"], kind="pattern", title="t3_derived")
    t4 = memory_factory(project["id"], kind="decision", title="t4_refined")

    for tgt, etype in [
        (t1, "depends_on"),
        (t2, "supersedes"),
        (t3, "derived_from"),
        (t4, "refined_by"),
    ]:
        insert_dep(conn, src["id"], tgt["id"], etype)

    edges = get_edges(conn, src["id"])
    edge_types = {e[2] for e in edges}
    assert "depends_on" in edge_types
    assert "supersedes" in edge_types
    assert "derived_from" in edge_types
    assert "refined_by" in edge_types
    assert len(edges) == 4

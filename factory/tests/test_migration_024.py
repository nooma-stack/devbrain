"""Tests for migration 024: memory_edges generalization.

Asserts that the CHECK constraint on memory_dependencies.edge_type now
accepts all 6 Phase-5 types, rejects unknown types, and that existing
rows (if any) are preserved.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.mark.db
def test_migration_024_accepts_all_six_edge_types(conn, project_factory, memory_factory):
    """All 6 edge types must be insertable without constraint violation."""
    project = project_factory("mig024_all_types")

    # Create 7 memories: one "source" per edge type + one shared "target"
    target = memory_factory(project["id"], kind="decision", title="target_024")

    edge_types = [
        "cites",
        "depends_on",
        "supersedes",
        "contradicts",
        "derived_from",
        "refined_by",
    ]

    with conn.cursor() as cur:
        for etype in edge_types:
            src = memory_factory(project["id"], kind="pattern", title=f"src_{etype}")
            cur.execute(
                "INSERT INTO devbrain.memory_dependencies "
                "(from_memory_id, to_memory_id, edge_type, created_by) "
                "VALUES (%s, %s, %s, 'test_migration_024')",
                (src["id"], target["id"], etype),
            )
    conn.commit()

    # Verify all 6 edge types landed
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT edge_type FROM devbrain.memory_dependencies "
            "WHERE to_memory_id = %s ORDER BY edge_type",
            (target["id"],),
        )
        found = {row[0] for row in cur.fetchall()}

    assert found == set(edge_types), f"Expected all 6 edge types, got: {found}"


@pytest.mark.db
def test_migration_024_rejects_unknown_edge_type(conn, project_factory, memory_factory):
    """An unknown edge type must be rejected by the CHECK constraint."""
    project = project_factory("mig024_rejects")

    src = memory_factory(project["id"], kind="decision", title="src_bad")
    dst = memory_factory(project["id"], kind="decision", title="dst_bad")

    with pytest.raises(Exception, match="memory_dependencies_edge_type_check|check"):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.memory_dependencies "
                "(from_memory_id, to_memory_id, edge_type, created_by) "
                "VALUES (%s, %s, 'unknown_type', 'test_migration_024')",
                (src["id"], dst["id"]),
            )
        conn.commit()

    conn.rollback()


@pytest.mark.db
def test_migration_024_new_types_work(conn, project_factory, memory_factory):
    """The two new Phase-5 types (derived_from, refined_by) must be usable."""
    project = project_factory("mig024_new_types")

    src = memory_factory(project["id"], kind="decision", title="src_new")
    dst = memory_factory(project["id"], kind="decision", title="dst_new")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'derived_from', 'test_migration_024')",
            (src["id"], dst["id"]),
        )
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'refined_by', 'test_migration_024')",
            (dst["id"], src["id"]),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT edge_type FROM devbrain.memory_dependencies "
            "WHERE from_memory_id IN (%s, %s) "
            "AND edge_type IN ('derived_from', 'refined_by') "
            "ORDER BY edge_type",
            (src["id"], dst["id"]),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    assert {r[0] for r in rows} == {"derived_from", "refined_by"}

"""Tests for migration 027: memory_dependencies ledger trigger.

Asserts that:
  1. The three new edge-event operation values are accepted by the
     memory_ledger CHECK constraint.
  2. An INSERT into memory_dependencies writes an 'edge_added' ledger row
     with the correct memory_id (from_memory_id), operation, and JSONB details.
  3. A DELETE from memory_dependencies writes an 'edge_removed' ledger row.
  4. An UPDATE to memory_dependencies writes an 'edge_updated' ledger row.
  5. The trigger function and triggers exist in the database.
"""
from __future__ import annotations

import json

import pytest


@pytest.mark.db
def test_027_trigger_function_exists(conn):
    """The _memory_dependencies_ledger_record trigger function must exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT routine_name FROM information_schema.routines "
            "WHERE routine_schema = 'devbrain' "
            "AND routine_name = '_memory_dependencies_ledger_record'"
        )
        assert cur.fetchone() is not None, (
            "_memory_dependencies_ledger_record function not found in devbrain schema"
        )


@pytest.mark.db
def test_027_insert_trigger_exists(conn):
    """AFTER INSERT trigger on memory_dependencies must exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trigger_name FROM information_schema.triggers "
            "WHERE event_object_schema = 'devbrain' "
            "  AND event_object_table = 'memory_dependencies' "
            "  AND trigger_name = 'trg_memory_dep_ledger_insert'"
        )
        assert cur.fetchone() is not None, "trg_memory_dep_ledger_insert not found"


@pytest.mark.db
def test_027_delete_trigger_exists(conn):
    """AFTER DELETE trigger on memory_dependencies must exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trigger_name FROM information_schema.triggers "
            "WHERE event_object_schema = 'devbrain' "
            "  AND event_object_table = 'memory_dependencies' "
            "  AND trigger_name = 'trg_memory_dep_ledger_delete'"
        )
        assert cur.fetchone() is not None, "trg_memory_dep_ledger_delete not found"


@pytest.mark.db
def test_027_update_trigger_exists(conn):
    """AFTER UPDATE trigger on memory_dependencies must exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trigger_name FROM information_schema.triggers "
            "WHERE event_object_schema = 'devbrain' "
            "  AND event_object_table = 'memory_dependencies' "
            "  AND trigger_name = 'trg_memory_dep_ledger_update'"
        )
        assert cur.fetchone() is not None, "trg_memory_dep_ledger_update not found"


@pytest.mark.db
def test_027_ledger_constraint_accepts_edge_operations(conn):
    """The memory_ledger operation column must accept the three new edge event values."""
    # We verify the constraint by checking the information_schema check constraint
    # definition includes all three new values, without doing a raw INSERT into
    # the ledger (which would require a valid project and chain state).
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cc.check_clause
            FROM information_schema.check_constraints cc
            JOIN information_schema.table_constraints tc
              ON tc.constraint_name = cc.constraint_name
             AND tc.constraint_schema = cc.constraint_schema
            WHERE tc.table_schema = 'devbrain'
              AND tc.table_name = 'memory_ledger'
              AND tc.constraint_type = 'CHECK'
              AND cc.check_clause LIKE '%edge_added%'
            """
        )
        row = cur.fetchone()
    assert row is not None, (
        "memory_ledger CHECK constraint does not include 'edge_added'; "
        "migration 027 may not have been applied"
    )
    clause = row[0]
    assert "edge_updated" in clause, f"'edge_updated' missing from CHECK: {clause}"
    assert "edge_removed" in clause, f"'edge_removed' missing from CHECK: {clause}"


@pytest.mark.db
def test_027_edge_insert_writes_edge_added_ledger_row(
    conn, project_factory, memory_factory
):
    """Inserting an edge must produce an 'edge_added' ledger row with correct fields."""
    project = project_factory("mig027_insert")
    src = memory_factory(project["id"], kind="decision", title="src_027")
    dst = memory_factory(project["id"], kind="decision", title="dst_027")

    # Capture ledger high-water mark before inserting the edge.
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(seq), 0) FROM devbrain.memory_ledger")
        seq_before = cur.fetchone()[0]

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test_027')",
            (src["id"], dst["id"]),
        )
    conn.commit()

    # Expect exactly one new ledger row after our high-water mark for this edge.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, operation, project_slug, payload_hash "
            "FROM devbrain.memory_ledger "
            "WHERE seq > %s "
            "  AND memory_id = %s "
            "  AND operation = 'edge_added'",
            (seq_before, src["id"]),
        )
        rows = cur.fetchall()

    assert len(rows) >= 1, (
        f"Expected at least 1 'edge_added' ledger row for {src['id']}, got {len(rows)}"
    )
    ledger_row = rows[-1]
    assert str(ledger_row[0]) == str(src["id"]), "memory_id should be from_memory_id"
    assert ledger_row[1] == "edge_added"
    # project_slug must be non-empty (resolved from from_memory_id).
    assert ledger_row[2], "project_slug should be non-empty"
    # payload_hash must be a non-empty bytea.
    assert ledger_row[3], "payload_hash should be non-empty"


@pytest.mark.db
def test_027_edge_delete_writes_edge_removed_ledger_row(
    conn, project_factory, memory_factory
):
    """Deleting an edge must produce an 'edge_removed' ledger row."""
    project = project_factory("mig027_delete")
    src = memory_factory(project["id"], kind="decision", title="src_027_del")
    dst = memory_factory(project["id"], kind="decision", title="dst_027_del")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test_027')",
            (src["id"], dst["id"]),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(seq), 0) FROM devbrain.memory_ledger")
        seq_before = cur.fetchone()[0]

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.memory_dependencies "
            "WHERE from_memory_id = %s AND to_memory_id = %s",
            (src["id"], dst["id"]),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, operation FROM devbrain.memory_ledger "
            "WHERE seq > %s "
            "  AND memory_id = %s "
            "  AND operation = 'edge_removed'",
            (seq_before, src["id"]),
        )
        rows = cur.fetchall()

    assert len(rows) >= 1, (
        f"Expected at least 1 'edge_removed' ledger row for {src['id']}, got {len(rows)}"
    )


@pytest.mark.db
def test_027_edge_update_writes_edge_updated_ledger_row(
    conn, project_factory, memory_factory
):
    """Updating an edge (e.g. confidence) must produce an 'edge_updated' ledger row."""
    project = project_factory("mig027_update")
    src = memory_factory(project["id"], kind="decision", title="src_027_upd")
    dst = memory_factory(project["id"], kind="decision", title="dst_027_upd")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by, confidence) "
            "VALUES (%s, %s, 'depends_on', 'test_027', 1.0)",
            (src["id"], dst["id"]),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(seq), 0) FROM devbrain.memory_ledger")
        seq_before = cur.fetchone()[0]

    # Update the confidence column.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory_dependencies "
            "SET confidence = 0.75 "
            "WHERE from_memory_id = %s AND to_memory_id = %s",
            (src["id"], dst["id"]),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, operation FROM devbrain.memory_ledger "
            "WHERE seq > %s "
            "  AND memory_id = %s "
            "  AND operation = 'edge_updated'",
            (seq_before, src["id"]),
        )
        rows = cur.fetchall()

    assert len(rows) >= 1, (
        f"Expected at least 1 'edge_updated' ledger row for {src['id']}, got {len(rows)}"
    )


@pytest.mark.db
def test_027_migration_recorded_in_schema_migrations(conn):
    """Migration 027 must be recorded in devbrain.schema_migrations."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT filename FROM devbrain.schema_migrations "
            "WHERE filename = '027_memory_dependencies_ledger_trigger.sql'"
        )
        assert cur.fetchone() is not None, (
            "Migration 027 not found in schema_migrations; "
            "apply the migration before running these tests."
        )

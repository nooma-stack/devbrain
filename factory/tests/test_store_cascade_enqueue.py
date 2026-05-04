"""Tests for the store() cascade-enqueue SQL pattern (Atlas Step 5e-NEW-1).

The actual cascade-enqueue helper lives in the MCP server (TypeScript) at
``mcp-server/src/memory.ts::enqueueCascades``. These tests exercise the
SAME SQL pattern from Python so the cascade-detection logic has Python-
side regression coverage even when no MCP runtime is available (e.g. CI
without a Node toolchain).

Each test sets up a depends_on edge and runs the enqueue INSERT directly,
mirroring what the TypeScript helper does. The integration of the
TypeScript code itself is validated end-to-end by P1
(test_p1_supersession_cascades.py) which proves the cascade triplet
lands in the queue when a supersedes edge is written.

Three tests, one per edge_type the queue's CHECK constraint accepts:
  - 'supersedes'    — superseding a row enqueues its dependents
  - 'archived_at'   — archiving a row enqueues its dependents
  - 'applies_when'  — mutating applies_when enqueues dependents

The SQL pattern is:

    INSERT INTO devbrain.curator_re_eval_queue
        (memory_id, cascade_source_id, edge_type)
    SELECT from_memory_id, $1, $2
    FROM devbrain.memory_dependencies
    WHERE to_memory_id = $1 AND edge_type = 'depends_on'
    ON CONFLICT (memory_id, cascade_source_id, edge_type)
        WHERE attempt_count < 3 DO NOTHING

Idempotency: a fourth test exercises the ON CONFLICT path — running the
INSERT twice yields exactly one queue row (the partial unique index
from migration 017 idx_re_eval_queue_dedup).
"""
from __future__ import annotations

import pytest

# The exact SQL fragment from mcp-server/src/memory.ts::enqueueCascades.
# Kept in sync by structural similarity (PR review checks both sides).
ENQUEUE_SQL = (
    "INSERT INTO devbrain.curator_re_eval_queue "
    "(memory_id, cascade_source_id, edge_type) "
    "SELECT from_memory_id, %s, %s "
    "FROM devbrain.memory_dependencies "
    "WHERE to_memory_id = %s AND edge_type = 'depends_on' "
    "ON CONFLICT (memory_id, cascade_source_id, edge_type) "
    "WHERE attempt_count < 3 DO NOTHING"
)


def _enqueue(conn, cascade_source_id, edge_type):
    """Python mirror of mcp-server enqueueCascades helper."""
    with conn.cursor() as cur:
        cur.execute(
            ENQUEUE_SQL, (cascade_source_id, edge_type, cascade_source_id)
        )
    conn.commit()


@pytest.mark.db
def test_supersedes_cascade_enqueues_dependents(
    conn, project_factory, memory_factory
):
    """Writing a supersedes edge over `old` enqueues every depends_on
    dependent of `old` with edge_type='supersedes'."""
    project = project_factory("scse_sup")
    old = memory_factory(project["id"], content="old")
    dep = memory_factory(project["id"], content="depends on old")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            (dep["id"], old["id"]),
        )
    conn.commit()

    _enqueue(conn, old["id"], "supersedes")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, cascade_source_id, edge_type "
            "FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s",
            (old["id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == dep["id"]
    assert rows[0][1] == old["id"]
    assert rows[0][2] == "supersedes"


@pytest.mark.db
def test_archived_at_cascade_enqueues_dependents(
    conn, project_factory, memory_factory
):
    """Setting archived_at on `src` enqueues every depends_on dependent
    of `src` with edge_type='archived_at'."""
    project = project_factory("scse_arch")
    src = memory_factory(project["id"], content="will be archived")
    dep = memory_factory(project["id"], content="depends on src")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            (dep["id"], src["id"]),
        )
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = NOW() WHERE id = %s",
            (src["id"],),
        )
    conn.commit()

    _enqueue(conn, src["id"], "archived_at")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, edge_type FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s",
            (src["id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == dep["id"]
    assert rows[0][1] == "archived_at"


@pytest.mark.db
def test_applies_when_cascade_enqueues_dependents(
    conn, project_factory, memory_factory
):
    """Mutating applies_when on `src` enqueues every depends_on dependent
    of `src` with edge_type='applies_when'."""
    project = project_factory("scse_aw")
    src = memory_factory(project["id"], content="changing applies_when")
    dep = memory_factory(project["id"], content="depends")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            (dep["id"], src["id"]),
        )
        cur.execute(
            "UPDATE devbrain.memory "
            "SET applies_when = '{\"language\": \"python\"}'::jsonb "
            "WHERE id = %s",
            (src["id"],),
        )
    conn.commit()

    _enqueue(conn, src["id"], "applies_when")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, edge_type FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s",
            (src["id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == dep["id"]
    assert rows[0][1] == "applies_when"


@pytest.mark.db
def test_enqueue_is_idempotent_via_partial_unique_index(
    conn, project_factory, memory_factory
):
    """ON CONFLICT against idx_re_eval_queue_dedup keeps the cascade
    penalty single-fire even under concurrent enqueues for the same
    triplet. Two _enqueue calls produce exactly one queue row."""
    project = project_factory("scse_idem")
    src = memory_factory(project["id"], content="src")
    dep = memory_factory(project["id"], content="dep")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            (dep["id"], src["id"]),
        )
    conn.commit()

    _enqueue(conn, src["id"], "supersedes")
    _enqueue(conn, src["id"], "supersedes")  # racing duplicate

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s",
            (src["id"],),
        )
        assert cur.fetchone()[0] == 1


@pytest.mark.db
def test_no_dependents_no_queue_rows(
    conn, project_factory, memory_factory
):
    """If `src` has no depends_on dependents, _enqueue is a no-op."""
    project = project_factory("scse_empty")
    src = memory_factory(project["id"], content="orphan")

    _enqueue(conn, src["id"], "supersedes")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s",
            (src["id"],),
        )
        assert cur.fetchone()[0] == 0

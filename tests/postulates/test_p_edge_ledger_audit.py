"""P_edge_ledger_audit — every memory_dependencies INSERT produces a ledger row.

POSTULATE
---------
Every INSERT into devbrain.memory_dependencies produces a corresponding
row in devbrain.memory_ledger with:
  - memory_id   = from_memory_id of the inserted edge
  - operation   = 'edge_added'

This is the audit guarantee introduced in migration 027. The ledger
trigger fires AFTER INSERT on memory_dependencies so the guaranteee is
enforced at the database level, not at application level.

SECONDARY ASSERTIONS
--------------------
The ledger row's project_slug must be non-empty (resolved from
from_memory_id → memory → projects). The payload_hash must be a
non-empty BYTEA.

STATUS
------
Activated by migration 027 (edge-level ledger trigger, Phase 5.x).
"""
from __future__ import annotations


def test_edge_insert_always_produces_ledger_row(
    conn, project_factory, memory_factory
):
    """Every memory_dependencies INSERT must produce an 'edge_added' ledger row."""
    project = project_factory("p_ela")
    src = memory_factory(project["id"], kind="decision", content="src for edge audit")
    dst = memory_factory(project["id"], kind="pattern", content="dst for edge audit")

    # Capture the ledger high-water mark before the edge insert.
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(seq), 0) FROM devbrain.memory_ledger")
        seq_before = cur.fetchone()[0]

    # Insert the edge.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            (src["id"], dst["id"]),
        )
    conn.commit()

    # The ledger must have a new 'edge_added' row for from_memory_id.
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
        f"Expected at least 1 'edge_added' ledger row for edge "
        f"from_memory_id={src['id']}, got {len(rows)}. "
        "Migration 027 may not have been applied."
    )

    ledger_row = rows[-1]
    memory_id, operation, project_slug, payload_hash = ledger_row

    assert str(memory_id) == str(src["id"]), (
        f"Ledger memory_id {memory_id!r} != from_memory_id {src['id']!r}"
    )
    assert operation == "edge_added"
    assert project_slug, "project_slug must be non-empty (resolved from project)"
    assert payload_hash, "payload_hash must be non-empty BYTEA"


def test_multiple_edge_inserts_each_produce_ledger_row(
    conn, project_factory, memory_factory
):
    """Each edge insert must produce its own distinct 'edge_added' ledger row."""
    project = project_factory("p_ela_multi")
    a = memory_factory(project["id"], content="a")
    b = memory_factory(project["id"], content="b")
    c = memory_factory(project["id"], content="c")

    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(seq), 0) FROM devbrain.memory_ledger")
        seq_before = cur.fetchone()[0]

    # Insert two edges.
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'postulate-test')",
            [(b["id"], a["id"]), (c["id"], b["id"])],
        )
    conn.commit()

    # Each edge must produce its own ledger row.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id FROM devbrain.memory_ledger "
            "WHERE seq > %s "
            "  AND operation = 'edge_added' "
            "  AND memory_id = ANY(%s::uuid[])",
            (seq_before, [str(b["id"]), str(c["id"])]),
        )
        found = {row[0] for row in cur.fetchall()}

    assert b["id"] in found, f"No 'edge_added' ledger row for b={b['id']}"
    assert c["id"] in found, f"No 'edge_added' ledger row for c={c['id']}"

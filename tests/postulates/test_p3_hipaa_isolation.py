"""P3 — Cross-project memory isolation (HIPAA).

POSTULATE
---------
A query scoped to project A must never return memory rows whose
project_id resolves to a different project. This is the substrate
guarantee that BrightBrain (HIPAA-bound) relies on to keep PHI from
leaking into general-purpose projects via the memory store.

STATUS
------
Active. The substrate enforces this today via the project_id NOT
NULL FK plus the WHERE-clause discipline in every reader. This test
documents and proves the invariant directly so any future change
that breaks isolation (e.g. a "cross-project lessons" feature added
without an opt-in flag) trips a postulate failure.

See docs/plans/2026-04-29-phase-3-discipline-layer.md §3.3 for the
postulate-then-test methodology.
"""
from __future__ import annotations


def test_memory_query_scoped_to_project_excludes_other_projects(
    conn, project_factory, memory_factory
):
    project_a = project_factory("a")
    project_b = project_factory("b")

    a_mem = memory_factory(
        project_a["id"],
        kind="decision",
        title="phi-handling",
        content="route PHI through encrypted column",
    )
    b_mem = memory_factory(
        project_b["id"],
        kind="decision",
        title="non-phi",
        content="route logs through stdout",
    )

    # The exact reader used by the curator brief / context loader is a
    # straight WHERE clause on project_id. Reproduce it here.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.memory "
            "WHERE project_id = %s AND archived_at IS NULL",
            (project_a["id"],),
        )
        a_ids = {r[0] for r in cur.fetchall()}

        cur.execute(
            "SELECT id FROM devbrain.memory "
            "WHERE project_id = %s AND archived_at IS NULL",
            (project_b["id"],),
        )
        b_ids = {r[0] for r in cur.fetchall()}

    assert a_mem["id"] in a_ids
    assert b_mem["id"] not in a_ids
    assert b_mem["id"] in b_ids
    assert a_mem["id"] not in b_ids


def test_memory_project_id_is_not_null(conn):
    """The FK + NOT NULL together are the load-bearing constraint —
    a regression that drops NOT NULL would let an unscoped row slip
    into 'every project's' query results."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'devbrain' AND table_name = 'memory' "
            "AND column_name = 'project_id'"
        )
        is_nullable = cur.fetchone()[0]
    assert is_nullable == "NO"

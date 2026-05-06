"""P_worker_multi_hop — cascade worker enqueues all transitive dependents in one pass.

POSTULATE
---------
When drain_one_batch() processes a queued memory B (which depends_on A),
and B itself has a transitive chain of dependents (C depends_on B,
D depends_on C), the worker enqueues ALL of C and D in the same drain
pass — not just the immediate 1-hop dependent.

This exercises the Phase 5.x multi-hop refactor where the old 1-hop SQL
JOIN was replaced with factory.graph.walker.walk(direction='incoming') to
enumerate all transitive dependents in one recursive CTE pass.

MECHANISM
---------
Before this refactor, a 3-node chain A→B→C→D required two drain cycles:
  Cycle 1: process B → enqueues C (1-hop)
  Cycle 2: process C → enqueues D (1-hop)

After this refactor, one drain cycle processes B and enqueues both C and D
simultaneously via the recursive CTE walker.

STATUS
------
Activated in Phase 5.x worker multi-hop refactor (PR fix/worker-ledger).
"""
from __future__ import annotations

import sys
from pathlib import Path

_FACTORY = Path(__file__).resolve().parents[2] / "factory"
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))

from curator.worker import drain_one_batch  # noqa: E402


def test_worker_enqueues_all_transitive_dependents_in_one_pass(
    conn, project_factory, memory_factory
):
    """A 3-hop chain A→B→C→D: processing B enqueues both C and D immediately."""
    project = project_factory("p_wmh")
    a = memory_factory(project["id"], content="a — root source")
    b = memory_factory(project["id"], content="b — depends on a")
    c = memory_factory(project["id"], content="c — depends on b")
    d = memory_factory(project["id"], content="d — depends on c")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.85 WHERE id IN (%s,%s,%s,%s)",
            (a["id"], b["id"], c["id"], d["id"]),
        )
        # Build the dependency chain: b→a, c→b, d→c
        cur.executemany(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            [
                (b["id"], a["id"]),
                (c["id"], b["id"]),
                (d["id"], c["id"]),
            ],
        )
        # Enqueue B for cascade (triggered by A being superseded)
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) "
            "VALUES (%s, %s, 'supersedes')",
            (b["id"], a["id"]),
        )
    conn.commit()

    # Single drain pass — should process B and enqueue C AND D transitively.
    drained = drain_one_batch(conn, batch_size=10)
    assert drained == 1, f"expected 1 drained (B), got {drained}"

    # Both C and D must be in the queue after one pass.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id FROM devbrain.curator_re_eval_queue "
            "WHERE memory_id = ANY(%s::uuid[])",
            ([str(c["id"]), str(d["id"])],),
        )
        enqueued = {row[0] for row in cur.fetchall()}

    assert c["id"] in enqueued, "C (1-hop transitive dependent) not enqueued in first pass"
    assert d["id"] in enqueued, "D (2-hop transitive dependent) not enqueued in first pass"


def test_worker_multi_hop_cycle_still_converges(
    conn, project_factory, memory_factory
):
    """Multi-hop walker does not cause infinite loops when a dependency cycle exists."""
    project = project_factory("p_wmhc")
    a = memory_factory(project["id"], content="a — cycle node 1")
    b = memory_factory(project["id"], content="b — cycle node 2")
    c = memory_factory(project["id"], content="c — depends on both")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.9 WHERE id IN (%s,%s,%s)",
            (a["id"], b["id"], c["id"]),
        )
        # A and B form a cycle; C depends on B
        cur.executemany(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            [
                (a["id"], b["id"]),   # a depends_on b
                (b["id"], a["id"]),   # b depends_on a (cycle)
                (c["id"], b["id"]),   # c depends_on b
            ],
        )
        # Enqueue A for cascade
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) "
            "VALUES (%s, %s, 'supersedes')",
            (a["id"], b["id"]),
        )
    conn.commit()

    # Drain repeatedly — must converge in finite passes.
    iterations = 0
    while iterations < 10:
        drained = drain_one_batch(conn, batch_size=50)
        if drained == 0:
            break
        iterations += 1

    assert iterations < 10, "multi-hop cascade with cycle did not converge"

    # Queue must be empty after convergence.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM devbrain.curator_re_eval_queue")
        remaining = cur.fetchone()[0]
    assert remaining == 0, f"queue not empty after convergence: {remaining} rows remain"

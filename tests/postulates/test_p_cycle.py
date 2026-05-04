"""P_cycle — dependency cycle (A -> B -> A) converges in one wave.

POSTULATE
---------
If A depends_on B and B depends_on A (cycle), a cascade triggered by
mutating A converges in a single drain pass — neither row is processed
more than once per cascade source. The mechanism is the
`last_cascade_at >= enqueued_at` guard in `_process_one`: once the
worker stamps `memory.last_cascade_at`, any later queue row pointing at
the same memory whose `enqueued_at` is older than that stamp is
short-circuited (DELETE without strength mutation, without further
propagation).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `from curator.worker import ...` resolve from tests/postulates/.
_FACTORY = Path(__file__).resolve().parents[2] / "factory"
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))

from curator.worker import drain_one_batch  # noqa: E402


def test_dependency_cycle_converges(conn, project_factory, memory_factory):
    project = project_factory("p_cycle")
    a = memory_factory(project["id"], content="a")
    b = memory_factory(project["id"], content="b")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.9 WHERE id IN (%s, %s)",
            (a["id"], b["id"]),
        )
        cur.executemany(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            [(a["id"], b["id"]), (b["id"], a["id"])],  # cycle
        )
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) "
            "VALUES (%s, %s, 'supersedes')",
            (a["id"], b["id"]),
        )
    conn.commit()

    # Drain repeatedly — should converge in finite passes.
    iterations = 0
    while iterations < 5:
        drained = drain_one_batch(conn, batch_size=50)
        if drained == 0:
            break
        iterations += 1
    assert iterations < 5, "cycle did not converge — possible infinite loop"

    # Queue should be empty.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM devbrain.curator_re_eval_queue")
        assert cur.fetchone()[0] == 0

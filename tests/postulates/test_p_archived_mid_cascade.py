"""P_archived_mid_cascade — archived target during drain → DELETE without propagating.

POSTULATE
---------
If a memory is archived after being enqueued for re-eval but before the
worker drains it, the worker DELETEs the queue row, does NOT update
strength, and does NOT propagate to the row's dependents. (Archived
rows are excluded from the planning brief by P2; further weakening of
their strength serves no purpose, and propagating to their dependents
would cascade an effectively-dead signal.)
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `from curator.worker import ...` resolve from tests/postulates/.
_FACTORY = Path(__file__).resolve().parents[2] / "factory"
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))

from curator.worker import drain_one_batch  # noqa: E402


def test_archived_target_no_propagation(conn, project_factory, memory_factory):
    project = project_factory("p_arch")
    a = memory_factory(project["id"], content="a")
    b = memory_factory(
        project["id"], content="b — depends on a, will archive"
    )
    c = memory_factory(
        project["id"], content="c — depends on b, should NOT be enqueued"
    )

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.85 "
            "WHERE id IN (%s,%s,%s)",
            (a["id"], b["id"], c["id"]),
        )
        cur.executemany(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            [(b["id"], a["id"]), (c["id"], b["id"])],
        )
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) "
            "VALUES (%s, %s, 'supersedes')",
            (b["id"], a["id"]),
        )
        # Archive b BEFORE worker drains.
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = NOW() WHERE id = %s",
            (b["id"],),
        )
    conn.commit()

    drain_one_batch(conn, batch_size=50)

    # c must NOT be enqueued.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.curator_re_eval_queue "
            "WHERE memory_id=%s",
            (c["id"],),
        )
        assert cur.fetchone()[0] == 0

    # b's strength NOT mutated.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strength FROM devbrain.memory WHERE id=%s", (b["id"],)
        )
        assert float(cur.fetchone()[0]) == 0.85

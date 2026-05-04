"""P_stuck_surface_able — queue rows with attempt_count >= 3 surface in CLI.

POSTULATE
---------
A queue row that has failed 3+ times is reported by
`devbrain curator queue-stuck` (i.e. by `curator.cli.list_stuck_queue_rows`)
with its memory_id, cascade_source_id, edge_type, attempt_count, and
last_error. This is the operator's escape hatch when the cascade worker
keeps failing on a particular row — visibility into which rows have
exhausted their retry budget is what lets the operator decide whether
to re-enqueue (after triage) or accept the stale strength.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `from curator.cli import ...` resolve from tests/postulates/.
_FACTORY = Path(__file__).resolve().parents[2] / "factory"
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))

from curator.cli import list_stuck_queue_rows  # noqa: E402


def test_stuck_rows_listed(conn, project_factory, memory_factory):
    project = project_factory("p_stuck")
    m = memory_factory(project["id"], content="will fail")
    src = memory_factory(project["id"], content="src")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type, attempt_count, "
            " last_error) "
            "VALUES (%s, %s, 'supersedes', 3, 'simulated failure')",
            (m["id"], src["id"]),
        )
    conn.commit()

    stuck = list_stuck_queue_rows(conn)
    # The connection is shared with the postulate suite (which may have
    # its own committed test rows mid-run); narrow to our specific
    # memory_id rather than asserting global cardinality.
    ours = [r for r in stuck if r["memory_id"] == m["id"]]
    assert len(ours) == 1
    assert ours[0]["attempt_count"] == 3
    assert ours[0]["last_error"] == "simulated failure"

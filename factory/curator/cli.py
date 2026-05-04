"""CLI surface for curator operations.

Pure-Python helpers that take an existing psycopg2 connection. The
Click subcommand wrappers live in factory/cli.py — they wire these
helpers up to a real DB connection and pretty-print the output.

Keeping the data-fetching code free of Click is deliberate so the
postulate test for P_stuck_surface_able can import it directly.
"""
from __future__ import annotations

from typing import Any


def list_stuck_queue_rows(conn: Any) -> list[dict]:
    """Return queue rows that have failed 3+ times.

    Used by `devbrain curator queue-stuck` and the P_stuck postulate.
    Rows are returned newest-source-first (FIFO drain order). Each row
    is a dict with keys: id, memory_id, cascade_source_id, edge_type,
    enqueued_at, attempt_count, last_error.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, memory_id, cascade_source_id, edge_type,
                   enqueued_at, attempt_count, last_error
            FROM devbrain.curator_re_eval_queue
            WHERE attempt_count >= 3
            ORDER BY enqueued_at
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

"""Cascade re-evaluation queue drainer.

Runs in the existing factory orchestrator process — no new daemon. Adds
one sibling poll alongside the existing factory_jobs poll.

Each batch:
1. SELECT ... FOR UPDATE SKIP LOCKED claims up to batch_size rows.
2. For each: load memory, compute new_strength, UPDATE memory, DELETE
   queue row.
3. Multi-hop: if penalty was significant (> MULTI_HOP_THRESHOLD after
   freshness decay), walk the row's own dependents and enqueue them.

On exception: increment attempt_count, persist last_error, leave queue
row in place. After 3 failures, the row is filtered out by both the
SELECT-FOR-UPDATE WHERE clause and the dedup partial unique index, so a
fresh enqueue of the same triplet is permitted once an operator has
triaged the failure.

Cycle prevention: each row carries an enqueued_at timestamp. The worker
also reads memory.last_cascade_at; if that timestamp is at-or-after the
queue row's enqueued_at the row is dropped without doing work — this
breaks dependency cycles (A -> B -> A) cleanly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from curator.strength import apply_cascade, cascade_penalty

logger = logging.getLogger(__name__)

# Multi-hop propagation threshold. If a cascade's penalty (after
# freshness decay) is smaller than this, don't bother propagating to the
# dependent's own dependents — too small to matter.
MULTI_HOP_THRESHOLD = Decimal("0.05")


def drain_one_batch(conn: Any, batch_size: int = 50) -> int:
    """Drain up to batch_size rows from curator_re_eval_queue.

    Returns the number of rows successfully drained (queue rows DELETEd).
    Failed rows stay in the queue with attempt_count incremented.
    """
    drained = 0
    with conn.cursor() as cur:
        # Claim a batch with SKIP LOCKED — multiple workers safe.
        cur.execute(
            """
            SELECT id, memory_id, cascade_source_id, edge_type, enqueued_at,
                   attempt_count
            FROM devbrain.curator_re_eval_queue
            WHERE attempt_count < 3
            ORDER BY enqueued_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (batch_size,),
        )
        rows = cur.fetchall()

    for row in rows:
        queue_id, memory_id, source_id, edge_type, enqueued_at, _attempts = row
        try:
            _process_one(
                conn, queue_id, memory_id, source_id, edge_type, enqueued_at
            )
            drained += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("drain_one_batch: row %s failed", queue_id)
            # Roll back any partial in-flight work from _process_one
            # before opening a fresh cursor for the failure-bookkeeping
            # UPDATE — otherwise psycopg2 raises InFailedTransaction.
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE devbrain.curator_re_eval_queue "
                    "SET attempt_count = attempt_count + 1, last_error = %s "
                    "WHERE id = %s",
                    (str(exc)[:1000], queue_id),
                )
            conn.commit()

    return drained


def _process_one(
    conn: Any,
    queue_id: Any,
    memory_id: Any,
    source_id: Any,
    edge_type: str,
    enqueued_at: datetime,
) -> None:
    """Process a single queue row in its own transaction."""
    with conn.cursor() as cur:
        # Load target memory.
        cur.execute(
            "SELECT strength, archived_at, last_cascade_at "
            "FROM devbrain.memory WHERE id = %s",
            (memory_id,),
        )
        result = cur.fetchone()
        if result is None:
            # Memory deleted — drop queue row.
            cur.execute(
                "DELETE FROM devbrain.curator_re_eval_queue WHERE id = %s",
                (queue_id,),
            )
            conn.commit()
            return
        strength, archived_at, last_cascade_at = result

        # Cycle prevention: skip if already cascaded since this source's
        # mutation. The wider-than-strict ">=" comparison means the row
        # is a no-op if anything cascaded after the source mutation,
        # which is exactly what we want for A->B->A dependency cycles.
        if last_cascade_at is not None and last_cascade_at >= enqueued_at:
            cur.execute(
                "DELETE FROM devbrain.curator_re_eval_queue WHERE id = %s",
                (queue_id,),
            )
            conn.commit()
            return

        # Archived — drop the queue row, don't update strength, don't
        # propagate to dependents. Archived rows are excluded from the
        # planning brief (P2) so further weakening is pointless.
        if archived_at is not None:
            cur.execute(
                "DELETE FROM devbrain.curator_re_eval_queue WHERE id = %s",
                (queue_id,),
            )
            conn.commit()
            return

        age_seconds = (
            datetime.now(timezone.utc) - enqueued_at
        ).total_seconds()
        new_strength = apply_cascade(strength, edge_type, age_seconds)
        penalty = cascade_penalty(edge_type, age_seconds)

        cur.execute(
            "UPDATE devbrain.memory "
            "SET strength = %s, last_cascade_at = NOW() "
            "WHERE id = %s",
            (new_strength, memory_id),
        )
        cur.execute(
            "DELETE FROM devbrain.curator_re_eval_queue WHERE id = %s",
            (queue_id,),
        )

        # Multi-hop propagation. Cycle prevention is two-layer:
        #
        #   1. Propagate the cascade wave's timestamp (this row's
        #      enqueued_at) into the new row instead of letting NOW()
        #      win. A multi-hop row is logically "from the same wave"
        #      as its parent, so its enqueued_at must match — that's
        #      what lets the `last_cascade_at >= enqueued_at` guard in
        #      _process_one short-circuit a repeat visit.
        #   2. Filter out dependents whose memory.last_cascade_at is
        #      at-or-after this row's enqueued_at — they were already
        #      touched by this same wave.
        #
        # Combined with the dedup partial unique index
        # (idx_re_eval_queue_dedup, partial WHERE attempt_count < 3) on
        # (memory_id, cascade_source_id, edge_type), this guarantees a
        # cycle (A->B->A) converges in one wave.
        if penalty > MULTI_HOP_THRESHOLD:
            cur.execute(
                "INSERT INTO devbrain.curator_re_eval_queue "
                "(memory_id, cascade_source_id, edge_type, enqueued_at) "
                "SELECT d.from_memory_id, %s, %s, %s "
                "FROM devbrain.memory_dependencies d "
                "JOIN devbrain.memory m ON m.id = d.from_memory_id "
                "WHERE d.to_memory_id = %s "
                "  AND d.edge_type = 'depends_on' "
                "  AND (m.last_cascade_at IS NULL "
                "       OR m.last_cascade_at < %s) "
                "ON CONFLICT (memory_id, cascade_source_id, edge_type) "
                "WHERE attempt_count < 3 DO NOTHING",
                (memory_id, edge_type, enqueued_at, memory_id, enqueued_at),
            )

    conn.commit()

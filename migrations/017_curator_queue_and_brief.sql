-- Atlas Step 5 — Curator agent + cascade re-evaluation queue
-- ============================================================================
--
-- Adds three substrate elements:
--   1. devbrain.curator_re_eval_queue — drained by the cascade worker. One
--      row per (dependent memory, source memory, edge type). Worker uses
--      SELECT ... FOR UPDATE SKIP LOCKED for safe concurrent drainage.
--   2. devbrain.memory.last_cascade_at — audit timestamp; set by the worker
--      every time it processes a row (whether or not strength changed).
--   3. devbrain.factory_jobs.curator_brief — JSONB snapshot of the brief
--      generated at QUEUED -> PLANNING. Every job phase reads the same
--      snapshot.
--
-- Foreign-key behavior: ON DELETE CASCADE for memory_id (if the dependent
-- memory is deleted, drop the queue row). For cascade_source_id we just
-- reference; the source could itself be archived but should still exist
-- as an audit anchor. If you need to delete a source row, drain the queue
-- first.

CREATE TABLE IF NOT EXISTS devbrain.curator_re_eval_queue (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id            UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    cascade_source_id    UUID NOT NULL REFERENCES devbrain.memory(id),
    edge_type            TEXT NOT NULL CHECK (edge_type IN
                            ('supersedes','archived_at','applies_when')),
    enqueued_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);

-- FIFO index for drainage order. Workers SELECT ... ORDER BY enqueued_at LIMIT N.
CREATE INDEX IF NOT EXISTS idx_re_eval_queue_fifo
    ON devbrain.curator_re_eval_queue (enqueued_at);

-- Skip rows that have failed too many times (worker filters with WHERE attempt_count < 3)
CREATE INDEX IF NOT EXISTS idx_re_eval_queue_unfailed
    ON devbrain.curator_re_eval_queue (enqueued_at)
    WHERE attempt_count < 3;

-- Dedup active queue rows. The cascade penalty is additive (not idempotent), so
-- two simultaneous enqueues for the same (dependent, source, edge_type) triplet
-- would double-penalize. The enqueue path uses INSERT ... ON CONFLICT DO NOTHING
-- against this index. Failed rows (attempt_count = 3) don't block legitimate
-- re-enqueues after they're surfaced and triaged.
CREATE UNIQUE INDEX IF NOT EXISTS idx_re_eval_queue_dedup
    ON devbrain.curator_re_eval_queue (memory_id, cascade_source_id, edge_type)
    WHERE attempt_count < 3;

-- Audit: last time the cascade worker touched this memory row.
ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS last_cascade_at TIMESTAMPTZ;

-- Cached brief — every phase of a factory job reads from this column.
ALTER TABLE devbrain.factory_jobs
    ADD COLUMN IF NOT EXISTS curator_brief JSONB;

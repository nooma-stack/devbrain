-- Atlas Step 6 — Refinement queue
-- ============================================================================
--
-- Signal #2 cases (NOT in brief but should have been) get queued here.
-- The curator's refinement pass at end of REVIEWING dequeues entries and
-- proposes applies_when widening for each.
--
-- Note: this was originally planned as migration 020, but migration 020
-- was used in Phase 6c for the `effective_hit_count` column on
-- devbrain.memory. The plan §Phase 6d uses 020; in this branch it's 021.

CREATE TABLE IF NOT EXISTS devbrain.refinement_queue (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id    UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    file_pattern TEXT,
    keywords     TEXT[],
    queued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at   TIMESTAMPTZ,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_refinement_queue_pending
    ON devbrain.refinement_queue (queued_at)
    WHERE applied_at IS NULL;

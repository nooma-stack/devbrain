-- Atlas Step 6 — Lesson graduation tracking
-- ============================================================================
--
-- Three columns + one index for the graduation pipeline:
--   1. current_streak — consecutive successful preventions (signal #3
--      increments, signal #1 resets)
--   2. graduated_at — timestamp when tier transitioned 'lesson' -> 'rule'
--   3. demoted_at — timestamp when tier transitioned 'rule' -> 'lesson'
--
-- Index optimizes the graduation candidate query at end of every
-- REVIEWING phase.

ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS current_streak INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS graduated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS demoted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_memory_graduation_candidates
    ON devbrain.memory (last_hit DESC)
    WHERE tier = 'lesson' AND current_streak >= 3 AND archived_at IS NULL;

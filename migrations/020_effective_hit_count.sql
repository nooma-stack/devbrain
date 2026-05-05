-- Atlas Step 6c — effective_hit_count column for graduation precision
-- ============================================================================
--
-- Adds devbrain.memory.effective_hit_count INTEGER NOT NULL DEFAULT 0.
--
-- This column was missed by migration 019 (Step 6a) but is required by the
-- three-signal feedback loop shipped in Step 6c (factory/curator/graduation.py):
--
--   * Signal #1 (in-brief AND failure): hit_count++, current_streak = 0
--   * Signal #3 (in-brief AND clean):   effective_hit_count++, current_streak++
--
-- Demotion precision = effective_hit_count / (hit_count + effective_hit_count).
-- A rule whose precision drops below 0.50 over a 30-day window gets demoted
-- back to tier='lesson'.

ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS effective_hit_count INTEGER NOT NULL DEFAULT 0;

-- Track this migration in schema_migrations (009 introduced the tracker).
INSERT INTO devbrain.schema_migrations (filename, applied_at)
VALUES ('020_effective_hit_count.sql', now())
ON CONFLICT (filename) DO NOTHING;

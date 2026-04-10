-- Migration 006: Replace WAITING state with BLOCKED; add dev-driven resolution.
-- ============================================================================

-- Add the resolution column for dev-driven unblocking
ALTER TABLE devbrain.factory_jobs
    ADD COLUMN IF NOT EXISTS blocked_resolution VARCHAR(20);
-- Values: 'proceed' | 'replan' | 'cancel' | NULL

-- Migrate any existing WAITING jobs to BLOCKED
UPDATE devbrain.factory_jobs
    SET status = 'blocked', current_phase = 'blocked'
    WHERE status = 'waiting';

-- Index on blocked_resolution for quick lookup of jobs with pending resolutions
CREATE INDEX IF NOT EXISTS idx_factory_jobs_blocked_resolution
    ON devbrain.factory_jobs(blocked_resolution)
    WHERE blocked_resolution IS NOT NULL;

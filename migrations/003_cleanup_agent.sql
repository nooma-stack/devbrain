-- Migration: Cleanup Agent Support
-- Adds archived_at column to factory_jobs and creates cleanup_reports table.
--
-- Usage:
--   docker exec -i devbrain-db psql -U devbrain -d devbrain < migrations/003_cleanup_agent.sql

-- ─── Factory Jobs: Archive Support ─────────────────────────────────────────────

ALTER TABLE devbrain.factory_jobs
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_factory_jobs_archived
    ON devbrain.factory_jobs(archived_at)
    WHERE archived_at IS NOT NULL;

-- ─── Dev Factory: Cleanup Reports ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS devbrain.factory_cleanup_reports (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                  UUID REFERENCES devbrain.factory_jobs(id) NOT NULL,
    report_type             VARCHAR(50) NOT NULL,
    outcome                 VARCHAR(50) NOT NULL,
    summary                 TEXT NOT NULL,
    phases_traversed        JSONB DEFAULT '[]',
    artifacts_summary       JSONB DEFAULT '{}',
    recovery_diagnosis      TEXT,
    recovery_action_taken   TEXT,
    time_elapsed_seconds    INT,
    metadata                JSONB DEFAULT '{}',
    created_at              TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cleanup_reports_job
    ON devbrain.factory_cleanup_reports(job_id);

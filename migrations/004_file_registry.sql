-- Migration: Multi-Dev File Registry
-- Adds file_locks table for coordinating concurrent edits across devs,
-- and extends factory_jobs with submitter tracking + blocking relationships.
--
-- Usage:
--   docker exec -i devbrain-db psql -U devbrain -d devbrain < migrations/004_file_registry.sql

-- ─── File Locks: One Active Lock Per File Per Project ─────────────────────────

CREATE TABLE IF NOT EXISTS devbrain.file_locks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES devbrain.factory_jobs(id) ON DELETE CASCADE NOT NULL,
    project_id      UUID REFERENCES devbrain.projects(id) NOT NULL,
    file_path       TEXT NOT NULL,
    dev_id          VARCHAR(255),
    locked_at       TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ DEFAULT (now() + interval '2 hours'),
    UNIQUE (project_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_file_locks_job
    ON devbrain.file_locks(job_id);

CREATE INDEX IF NOT EXISTS idx_file_locks_project
    ON devbrain.file_locks(project_id);

CREATE INDEX IF NOT EXISTS idx_file_locks_expires
    ON devbrain.file_locks(expires_at);

-- ─── Factory Jobs: Submitter + Blocking Relationships ─────────────────────────

ALTER TABLE devbrain.factory_jobs
    ADD COLUMN IF NOT EXISTS submitted_by VARCHAR(255);

ALTER TABLE devbrain.factory_jobs
    ADD COLUMN IF NOT EXISTS blocked_by_job_id UUID REFERENCES devbrain.factory_jobs(id);

CREATE INDEX IF NOT EXISTS idx_factory_jobs_blocked_by
    ON devbrain.factory_jobs(blocked_by_job_id)
    WHERE blocked_by_job_id IS NOT NULL;

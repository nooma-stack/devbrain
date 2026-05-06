-- Migration 025: cognify_run_log table
-- Tracks per-pass cognify runs for idempotency + observability.
-- All rows include project_id for P3 isolation (cross-project queries
-- filter by project_id — never leak across project boundaries).
-- No raw PHI is stored here: only pass metadata and row counts.
--
-- Phase 6 design: docs/plans/2026-05-05-phase-6-cognify-memify-design.md §7

CREATE TABLE devbrain.cognify_run_log (
    id              BIGSERIAL PRIMARY KEY,
    pass_name       TEXT NOT NULL,                          -- 'extract', 'decay', etc.
    project_id      UUID REFERENCES devbrain.projects(id),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    rows_processed  INTEGER,
    llm_calls       INTEGER DEFAULT 0,
    error           TEXT,
    metadata        JSONB
);

CREATE INDEX idx_cognify_run_log_pass_started
    ON devbrain.cognify_run_log (pass_name, started_at DESC);

CREATE INDEX idx_cognify_run_log_project_pass
    ON devbrain.cognify_run_log (project_id, pass_name, started_at DESC);

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('025_cognify_run_log.sql');

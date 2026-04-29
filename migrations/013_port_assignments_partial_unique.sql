-- DevBrain Port Registry — partial UNIQUE for project history
-- ============================================================================
--
-- Migration 012 shipped `UNIQUE (project_id, purpose)` on port_assignments.
-- That makes sense for ACTIVE assignments (one port per purpose at a time)
-- but blocks a project from ever re-assigning the same purpose after a
-- retirement.
--
-- Concrete scenario: project X is retired (rows flipped archived_at = now()).
-- Project X is later spun back up. Agent runs `devbrain assign-port --slug X
-- --purpose api ...` — fails because the archived row still satisfies
-- (project_id=X, purpose=api) and blocks the new INSERT via UNIQUE.
--
-- Fix: drop the strict UNIQUE constraint and replace it with a partial
-- UNIQUE INDEX that only enforces uniqueness on non-archived rows. Archived
-- rows can stack up — they're the project's port history, queryable via
-- `devbrain ports --project X --include-archived` (default).
--
-- This preserves historical attribution (a project can look up "what port
-- did I use to use for purpose=api?") while letting the project re-assign
-- that purpose to a different port post-revival.

BEGIN;

-- Drop the column-level UNIQUE constraint from migration 012.
-- Postgres auto-names these as <table>_<column>_<column>_..._key.
ALTER TABLE devbrain.port_assignments
    DROP CONSTRAINT IF EXISTS port_assignments_project_id_purpose_key;

-- Replace with a partial UNIQUE INDEX scoped to active assignments only.
CREATE UNIQUE INDEX IF NOT EXISTS uq_port_assignments_project_purpose_active
    ON devbrain.port_assignments (project_id, purpose)
    WHERE archived_at IS NULL;

INSERT INTO devbrain.schema_migrations (filename, applied_at)
VALUES ('013_port_assignments_partial_unique.sql', now())
ON CONFLICT (filename) DO NOTHING;

COMMIT;

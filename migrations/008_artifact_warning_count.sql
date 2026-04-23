-- Migration 008: Add warning_count to factory_artifacts.
-- ============================================================================
--
-- Plumbing-only addition. Review phases already count BLOCKING findings via
-- blocking_count; warning_count adds the mirror counter for WARNING-severity
-- findings so downstream surfaces can display both counts without re-parsing
-- artifact content. No behavior change — counts are stored but not yet acted
-- on by transition logic.
--
-- Usage:
--   docker exec -i devbrain-db psql -U devbrain -d devbrain < migrations/008_artifact_warning_count.sql

ALTER TABLE devbrain.factory_artifacts
    ADD COLUMN IF NOT EXISTS warning_count INT DEFAULT 0;

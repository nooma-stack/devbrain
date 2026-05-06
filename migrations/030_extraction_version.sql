-- Migration 030: extraction_version column on devbrain.memory
-- ============================================================================
-- Supports versioned re-extraction in Phase 6 (cognify_extract). When the
-- extraction prompt or model is bumped, CURRENT_EXTRACTION_VERSION in
-- factory/cognify/extract.py is incremented. The reextract_cli --since-version
-- flag then selects rows with extraction_version < N for re-processing.
--
-- Default 1: all existing extracted rows (tier='lesson') get version=1 implicitly
-- (DEFAULT 1 on ADD COLUMN backfills existing rows).
--
-- Non-extracted rows (tier='memory', tier='rule') also receive 1 as the
-- default, but the column is only meaningful for tier='lesson' rows.
--
-- Phase 6 design: docs/plans/2026-05-05-phase-6-cognify-memify-design.md §6

ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS extraction_version INTEGER DEFAULT 1;

COMMENT ON COLUMN devbrain.memory.extraction_version IS
    'Version of the extraction prompt/model that produced this row. '
    'Bumped in factory/cognify/extract.py CURRENT_EXTRACTION_VERSION when '
    'the extraction logic changes. Used by reextract_cli --since-version. '
    'Only meaningful for tier=''lesson'' rows; non-extracted rows carry the '
    'default value (1).';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('030_extraction_version.sql')
ON CONFLICT (filename) DO NOTHING;

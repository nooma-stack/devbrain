-- Migration 035: dedup the 5 seeded compliance rules on the devbrain project
-- ============================================================================
-- Migration 023 seeded 5 regulatory rules (FERPA / HIPAA / two SOC2 / a
-- general PHI redaction rule) onto the canonical `devbrain` project so
-- they could surface cross-project to opted-in profiles (see PR #91).
-- Subsequent test runs and re-imports reintroduced these rows multiple
-- times. By 2026-05-14 the active table held 24 copies of each rule
-- (120 active rule rows, all created 2026-05-05).
--
-- Migration 033 removed the rows with the NULL-profiles pollution
-- signature, but the 120 surviving rows have non-NULL compliance_profiles
-- and are semantically identical within each `content` value:
--   * Same compliance_profiles array
--   * Same title
--   * Same NULL applies_when
--   * Same created_at calendar day (the seed migration day)
--   * Zero FK references from memory_dependencies, curator_re_eval_queue,
--     or refinement_queue
--
-- This migration collapses each rule's 24 duplicates to 1 row using
-- ROW_NUMBER() partitioned by `content`, keeping the row with the
-- earliest `created_at` (id as tiebreaker for same-microsecond ties).
-- Result: 5 rule rows on the devbrain project.
--
-- The ledger AFTER-DELETE trigger fires normally — these are
-- legitimate deletes of pollution duplicates, and 115 ledger entries
-- is a negligible audit trail addition.
--
-- Idempotent: a second run finds 1 row per content (rn=1 for all),
-- the WHERE rn > 1 set is empty, DELETE affects zero rows.

BEGIN;

WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY content
               ORDER BY created_at, id
           ) AS rn
    FROM devbrain.memory
    WHERE project_id = (
              SELECT id FROM devbrain.projects WHERE slug = 'devbrain'
          )
      AND archived_at IS NULL
      AND kind = 'decision'
      AND tier = 'rule'
)
DELETE FROM devbrain.memory
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('035_dedup_seeded_rules.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

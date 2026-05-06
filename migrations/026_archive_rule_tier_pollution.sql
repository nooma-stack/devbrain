-- ─────────────────────────────────────────────────────────────────────────────
-- 026: Archive tier='rule' rows that lack compliance_profiles
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Background. As of Phase 7c (migration 023) only rows with non-empty
-- compliance_profiles can legitimately surface as rules in any project's
-- curator brief. Per P6 semantics: a rule with empty/NULL
-- compliance_profiles is invisible to all projects — explicit opt-in.
-- Such rows are functionally inert.
--
-- During heavy autonomous-worker traffic on 2026-05-05, ~35k rule-tier
-- rows accumulated in the canonical 'devbrain' project with NULL
-- compliance_profiles. Source is unclear (graduated_at is NULL on these
-- rows, so they didn't go through factory/cognify/strengthen.py;
-- multiple writer-path candidates remain under investigation). They
-- have no functional effect (P6 invisibility) but they pollute the
-- rules_lint discovery scan and confuse counts.
--
-- This migration archives them (sets archived_at = NOW()) so they're
-- excluded from all readers (curator brief, walker, deep_search) and
-- the rules_lint scan. We do NOT DELETE — preserves the audit trail
-- and matches GC policy from Phase 6 design (archive only, never delete).
--
-- The 5 legitimately seeded rules from migration 023 are unaffected —
-- they have non-empty compliance_profiles so the WHERE clause excludes
-- them.

-- Step 1: archive rule rows that lack compliance_profiles (the
-- pollution).
UPDATE devbrain.memory
SET archived_at = NOW()
WHERE tier = 'rule'
  AND (compliance_profiles IS NULL OR array_length(compliance_profiles, 1) = 0)
  AND archived_at IS NULL;

-- Step 2: dedup legitimate rule rows. Migration 023 ran multiple times
-- on this DB at some prior point because its INSERT ... ON CONFLICT
-- DO NOTHING relies on a unique constraint that doesn't apply to the
-- columns it inserts (provenance_id is NULL on seed rows; the partial
-- unique index `(provenance_id, kind) WHERE provenance_id IS NOT NULL`
-- never fires). Each application created fresh duplicates of the 5
-- seeded rules.
--
-- Strategy: per (project_id, title) within tier='rule', keep only the
-- oldest row; archive the rest. The remaining row preserves the original
-- seed semantics (compliance_profiles, content). Cleanest dedup since
-- all dupes have identical content.
WITH ranked_rules AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY project_id, title
               ORDER BY created_at ASC
           ) AS rn
    FROM devbrain.memory
    WHERE tier = 'rule'
      AND archived_at IS NULL
      AND compliance_profiles IS NOT NULL
      AND array_length(compliance_profiles, 1) > 0
)
UPDATE devbrain.memory m
SET archived_at = NOW()
FROM ranked_rules r
WHERE m.id = r.id AND r.rn > 1;

-- Comment on the migration's effect for future readers.
COMMENT ON TABLE devbrain.memory IS
    'Phase 2 unified memory store. Migration 026 archived tier=rule + '
    'NULL compliance_profiles rows (pollution from autonomous worker '
    'traffic 2026-05-05) AND deduplicated legitimate seed rule rows '
    '(migration 023 had a non-functional ON CONFLICT clause) — keeping '
    'the oldest row per (project_id, title) within tier=rule.';

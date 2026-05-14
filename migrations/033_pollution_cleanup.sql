-- Migration 033: clean up rule-pollution + test-pollution projects
-- ============================================================================
-- One-time data cleanup, idempotent. Three independent deletions:
--
--   1. Rule-tier pollution (decision/rule rows with NULL compliance_profiles,
--      NULL applies_when, NULL provenance_id) — the signature from the
--      historical export/import column-drift bug (factory/export_memory.py
--      + factory/import_memory.py dropped 6 columns on roundtrip, causing
--      each `devbrain export-memory | devbrain import-memory` cycle to
--      duplicate the 5 seeded compliance rules with NULL profiles).
--      Migration 026 archived the first ~35K such rows; subsequent
--      pollution events produced millions more. Catches both archived
--      and still-active polluted rows. Real seeded rules (from migration
--      023) have non-NULL compliance_profiles and are NOT touched.
--
--   2. Test-pollution projects (slugs matching `(factory|postulate)_test_*`)
--      created by factory/postulate test runs that didn't clean up after
--      themselves. ~7.5M memory rows across 14 such projects. Memory FKs
--      cascade automatically (curator_re_eval_queue, memory_dependencies,
--      refinement_queue); cognify_run_log doesn't cascade so we delete
--      its rows explicitly. The empty project rows are removed last.
--
--   3. The memory-ledger AFTER-DELETE trigger is disabled for the
--      duration of the migration so we don't generate ~14M ledger
--      entries auditing this one-shot cleanup. The migration file
--      itself is the audit record (checked into git, applied with
--      timestamp recorded in schema_migrations).
--
-- Idempotent: re-running on a clean DB is a no-op. New devbrain installs
-- have no pollution to delete; the DELETE statements simply affect zero
-- rows.

-- Step 1: silence the audit trigger so the bulk DELETE doesn't write
-- millions of ledger entries. Re-enabled at the end. This is a one-shot
-- cleanup; the trigger's audit purpose is for live writes, not
-- archaeology.
ALTER TABLE devbrain.memory DISABLE TRIGGER trg_memory_ledger_delete;

-- Step 2: hard-delete rule-tier pollution by signature. Real seeded
-- rules (migration 023) have compliance_profiles populated; the
-- pollution pattern leaves all three nullable fields empty.
DELETE FROM devbrain.memory
WHERE kind = 'decision'
  AND tier = 'rule'
  AND compliance_profiles IS NULL
  AND applies_when IS NULL
  AND provenance_id IS NULL;

-- Step 3: delete memory rows for test-pollution projects. FK cascades
-- handle curator_re_eval_queue, memory_dependencies, refinement_queue.
DELETE FROM devbrain.memory
WHERE project_id IN (
  SELECT id FROM devbrain.projects
  WHERE slug ~ '^(factory|postulate)_test_'
);

-- Step 4: clean tables FK-pointing at projects (NO ACTION delete rule)
-- that don't cascade from memory, in dependency order (children first).
-- Only the tables that actually have rows for these test projects need
-- touching — the rest are zero. notifications.job_id is SET NULL on
-- delete, so it doesn't need explicit cleanup; factory_jobs.file_locks
-- relationship is CASCADE, also fine.
DELETE FROM devbrain.factory_artifacts
WHERE job_id IN (
  SELECT id FROM devbrain.factory_jobs
  WHERE project_id IN (
    SELECT id FROM devbrain.projects
    WHERE slug ~ '^(factory|postulate)_test_'
  )
);
DELETE FROM devbrain.cognify_run_log
WHERE project_id IN (
  SELECT id FROM devbrain.projects
  WHERE slug ~ '^(factory|postulate)_test_'
);
DELETE FROM devbrain.factory_jobs
WHERE project_id IN (
  SELECT id FROM devbrain.projects
  WHERE slug ~ '^(factory|postulate)_test_'
);

-- Step 5: drop the empty test-pollution projects themselves.
DELETE FROM devbrain.projects
WHERE slug ~ '^(factory|postulate)_test_';

-- Step 6: re-enable the audit trigger before commit.
ALTER TABLE devbrain.memory ENABLE TRIGGER trg_memory_ledger_delete;

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('033_pollution_cleanup.sql')
ON CONFLICT (filename) DO NOTHING;

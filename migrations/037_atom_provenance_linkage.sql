-- Migration 037: link extracted atoms back to their source session via provenance_id
-- ============================================================================
-- factory/cognify/extract.py:_upsert_memory has historically inserted
-- pattern/decision/lesson/issue rows with provenance_id=NULL and
-- stored the source session as applies_when->>'source_session'
-- (jsonb). The original reason was the (provenance_id, kind) UNIQUE
-- constraint from migration 011 — a session can produce N patterns
-- and N decisions, but the constraint would only allow one each.
--
-- That workaround broke the per-session chain at the memory layer:
--   * discover_sessions_needing_atomization can't use the NOT EXISTS
--     join on memory.provenance_id to skip already-atomized sessions
--     (rows have NULL provenance), so it returns the same sessions
--     forever; idempotency falls to title-match in _upsert_memory.
--   * graph_walk and deep_search's supersession-aware queries can't
--     find the session ancestor of an atom via the standard FK join.
--
-- After migration 032 narrowed the unique index to exclude chunks,
-- the residual problem is that atom kinds still can't share a
-- provenance_id. This migration replaces the constraint with a
-- title-aware shape that allows N atoms per (session, kind) while
-- still preventing exact-duplicate re-extractions, then backfills
-- existing atoms' provenance_id from their applies_when.source_session.
--
-- Schema delta:
--
--   - DROP idx_memory_provenance_kind_unique (the broad atom index).
--   - CREATE idx_memory_session_summary_unique on (provenance_id, kind)
--     WHERE kind='session_summary'. One summary per session stays
--     enforced.
--   - CREATE idx_memory_atom_title_unique on
--     (provenance_id, kind, title) WHERE kind IN
--     ('pattern','decision','lesson','issue'). Multiple atoms per
--     (session, kind) allowed; identical re-extractions still fold.
--
-- Backfill:
--
--   - UPDATE memory SET provenance_id = applies_when->>'source_session'
--     WHERE provenance_id IS NULL AND kind IN
--     ('pattern','decision','lesson','issue') AND applies_when ?
--     'source_session'.
--   - Pre-verified zero (provenance_id, kind, title) duplicates would
--     result on the brightbot data we have today.
--
-- Code companions in the same PR:
--   - factory/cognify/extract.py:_upsert_memory now sets
--     provenance_id=session_id on INSERT; idempotency check uses
--     provenance_id + title (not applies_when path).
--   - ingest/memory_writer.py ON CONFLICT clause matches the new
--     index predicates.
--
-- Idempotent: re-running on a clean DB rewrites the same rows with
-- the same values (a no-op effectively), and the DROP/CREATE INDEX
-- pattern uses IF EXISTS / IF NOT EXISTS where postgres supports it.

BEGIN;

-- Step 1: drop the broad unique index. Both new indexes will replace
-- its coverage with more precise predicates.
DROP INDEX IF EXISTS devbrain.idx_memory_provenance_kind_unique;

-- Step 2: backfill atom provenance from applies_when.source_session.
-- Pre-flight on the laptop verified: 6,636 atoms have valid
-- source_session pointers, all linking to real raw_sessions, with
-- zero (session, kind, title) collisions. Safe to UPDATE in bulk.
UPDATE devbrain.memory
SET provenance_id = (applies_when->>'source_session')::uuid
WHERE provenance_id IS NULL
  AND kind IN ('pattern', 'decision', 'lesson', 'issue')
  AND applies_when ? 'source_session'
  AND (applies_when->>'source_session') ~
      '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

-- Step 3: session_summary stays one-per-session. Same predicate as
-- the pre-migration index, scoped to a single kind.
CREATE UNIQUE INDEX idx_memory_session_summary_unique
    ON devbrain.memory (provenance_id, kind)
    WHERE provenance_id IS NOT NULL
      AND kind = 'session_summary';

COMMENT ON INDEX devbrain.idx_memory_session_summary_unique IS
    'Migration 037: one session_summary row per session. Enforces the '
    'invariant that re-summarizing a session yields a single row, not '
    'a new one per run. Chunks are not in scope.';

-- Step 4: atoms (pattern/decision/lesson/issue) are unique by
-- (session, kind, title). Multiple atoms per (session, kind) allowed,
-- but identical re-extractions fold via ON CONFLICT DO NOTHING.
CREATE UNIQUE INDEX idx_memory_atom_title_unique
    ON devbrain.memory (provenance_id, kind, title)
    WHERE provenance_id IS NOT NULL
      AND kind IN ('pattern', 'decision', 'lesson', 'issue');

COMMENT ON INDEX devbrain.idx_memory_atom_title_unique IS
    'Migration 037: atoms (pattern/decision/lesson/issue) are unique '
    'per (provenance_id, kind, title). Allows N atoms per (session, '
    'kind) — a session can produce many decisions, many patterns — '
    'while still folding exact-duplicate re-extractions. NULL title '
    'rows coexist (NULLs are not equal in unique indexes).';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('037_atom_provenance_linkage.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

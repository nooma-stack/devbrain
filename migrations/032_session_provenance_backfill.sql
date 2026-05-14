-- Migration 032: backfill memory.provenance_id to the SOURCE session UUID
-- ============================================================================
-- Bug fix: ingest/db.py:insert_chunk() was dual-writing chunks into
-- devbrain.memory with `provenance_id = chunk_id` (the chunk row's own
-- UUID) instead of `provenance_id = chunks.source_id` (the originating
-- raw_session UUID).
--
-- Consequence: every chunk became its own "session" at the memory layer.
-- Phase 6's discover_sessions_needing_atomization (which groups by
-- memory.provenance_id) saw 48,451 brightbot "sessions" each with a
-- single chunk — instead of the real ~455 raw_sessions with many
-- chunks per session. Atomization ran (or would have run) at chunk
-- granularity, producing shallow atoms from isolated text fragments.
--
-- Fix structure:
--   1. The existing unique index (provenance_id, kind) WHERE provenance_id
--      IS NOT NULL was designed assuming each chunk had a unique
--      provenance_id. After the backfill, many chunks share one session's
--      provenance_id, so the constraint must be loosened to apply only to
--      atom kinds (pattern/decision/lesson/issue/session_summary).
--   2. Backfill UPDATE: memory.provenance_id := chunks.source_id where
--      the chunk linkage exists and the value would actually change.
--      Chunks that had source_id=NULL (e.g. migrate_openclaw_memory.py
--      imports) keep their existing provenance_id since there's nothing
--      to point at.
--
-- Idempotent: re-running this migration is a no-op. The DROP INDEX uses
-- IF EXISTS, the UPDATE skips rows that are already correct, and the
-- final INSERT into schema_migrations uses ON CONFLICT DO NOTHING.
--
-- Note for record_memory() callers: the partial unique index predicate
-- changed from `WHERE provenance_id IS NOT NULL` to
-- `WHERE provenance_id IS NOT NULL AND kind != 'chunk'`. The ON CONFLICT
-- clause in ingest/memory_writer.py must be updated to match — that
-- code change ships in the same PR as this migration.

-- Step 1: replace the unique index. The old shape forbade duplicate
-- (provenance_id, kind='chunk') tuples, which the bug accidentally
-- preserved by giving each chunk its own provenance. Post-backfill,
-- many chunks legitimately share a session's provenance_id.
DROP INDEX IF EXISTS devbrain.idx_memory_provenance_kind_unique;

CREATE UNIQUE INDEX idx_memory_provenance_kind_unique
    ON devbrain.memory (provenance_id, kind)
    WHERE provenance_id IS NOT NULL AND kind != 'chunk';

COMMENT ON INDEX devbrain.idx_memory_provenance_kind_unique IS
    'Atoms (pattern/decision/lesson/issue/session_summary) are unique '
    'per (provenance_id, kind). Chunks are NOT — a single session has '
    'many chunks all sharing one provenance_id. Migration 032 narrowed '
    'this from the original (which assumed one row per provenance) when '
    'the chunk-provenance bug was fixed.';

-- Step 2: backfill. Rewrite memory.provenance_id where it currently
-- points at a chunks.id (the bug) to point at chunks.source_id
-- (the originating session). Idempotent via the inequality predicate.
UPDATE devbrain.memory m
SET provenance_id = c.source_id
FROM devbrain.chunks c
WHERE c.id = m.provenance_id
  AND c.source_id IS NOT NULL
  AND m.provenance_id != c.source_id;

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('032_session_provenance_backfill.sql')
ON CONFLICT (filename) DO NOTHING;

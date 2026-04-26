-- Migration 011: idempotency constraint for devbrain.memory dual-writes.
-- ============================================================================
--
-- Adds a partial UNIQUE index on (provenance_id, kind) so the P2.b adapter
-- helpers (ingest/memory_writer.py and mcp-server/src/memory.ts) can use
-- ON CONFLICT (provenance_id, kind) WHERE provenance_id IS NOT NULL
-- DO NOTHING for race-free idempotency. Postgres requires that the
-- INSERT's WHERE predicate match the index's predicate exactly to infer
-- the constraint, so both clauses include `WHERE provenance_id IS NOT NULL`.
--
-- Why partial: rows without a provenance_id (e.g. ad-hoc curator entries
-- in the future) are not deduplicated by source — they're allowed to
-- multiply. The legacy code already inserts unconditionally; carrying
-- that semantics through to memory keeps P2.b strictly write-additive.
--
-- Why split from 010: 010 was scope-limited to "table exists, no writers".
-- The constraint had no callers in P2.a and would have been dead code.
-- P2.b needs it now because dual-writes are racy (two ingest workers may
-- process the same chunk concurrently) and pre-insert existence checks
-- are slower and still racy.
--
-- Idempotent (CREATE UNIQUE INDEX IF NOT EXISTS) — re-running on a DB
-- that already has the constraint is a no-op. Required by the
-- schema_migrations bootstrap path which may re-apply on upgrade.
--
-- Usage:
--   bin/devbrain migrate

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_provenance_kind_unique
    ON devbrain.memory (provenance_id, kind)
    WHERE provenance_id IS NOT NULL;

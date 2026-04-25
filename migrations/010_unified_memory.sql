-- Migration 010: devbrain.memory unified memory table.
-- ============================================================================
--
-- PURE ADDITIVE — chunks/decisions/patterns/issues are unchanged. P2.b will
-- dual-write into devbrain.memory alongside the legacy tables, P2.c will
-- backfill historical rows, and P2.d will drop the legacy tables once
-- readers have switched over.
--
-- Until P2.b ships this table is empty and no production code path reads
-- from it. Schema verification only — no data migration here.
--
-- Idempotent (CREATE … IF NOT EXISTS), matching the convention 004-007
-- established for migrations layered on top of the schema_migrations
-- runner: re-running on a DB that already has the table is a no-op.
--
-- Usage:
--   docker exec -i devbrain-db psql -U devbrain -d devbrain < migrations/010_unified_memory.sql

CREATE TABLE IF NOT EXISTS devbrain.memory (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id      UUID NOT NULL REFERENCES devbrain.projects(id),
    kind            TEXT NOT NULL CHECK (kind IN ('chunk', 'decision', 'pattern', 'issue', 'session_summary')),
    title           TEXT,
    content         TEXT NOT NULL,
    embedding       vector(1024),
    strength        NUMERIC NOT NULL DEFAULT 1.0,
    hit_count       INTEGER NOT NULL DEFAULT 0,
    last_hit        TIMESTAMPTZ,
    applies_when    JSONB,
    -- Loose pointer to the originating row (raw_sessions, factory_jobs, …);
    -- intentionally no FK because the source spans multiple tables.
    provenance_id   UUID,
    tier            TEXT NOT NULL DEFAULT 'memory' CHECK (tier IN ('memory', 'lesson', 'rule')),
    archived_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_project_kind ON devbrain.memory (project_id, kind)
    WHERE archived_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_embedding ON devbrain.memory
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_memory_strength ON devbrain.memory (strength DESC)
    WHERE archived_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_applies_when ON devbrain.memory
    USING GIN (applies_when);

CREATE INDEX IF NOT EXISTS idx_memory_provenance ON devbrain.memory (provenance_id)
    WHERE provenance_id IS NOT NULL;

COMMENT ON TABLE devbrain.memory IS
    'Phase 2 unified memory store. Consolidates chunks/decisions/patterns/issues. Adapters land in P2.b; backfill in P2.c.';

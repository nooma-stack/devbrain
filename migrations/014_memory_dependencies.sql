-- ─────────────────────────────────────────────────────────────────────────────
-- 014: memory_dependencies — typed edges between memory rows
-- ─────────────────────────────────────────────────────────────────────────────
-- Phase 3 / Atlas Step 1 (see docs/plans/2026-04-29-phase-3-discipline-layer.md
-- and PR #67 for the full design). Adds the edge table + indexes + backfills
-- supersession edges from the legacy decisions.superseded_by chain.
--
-- Empty for graph traversal until either:
--   1. callers pass `depends_on` / `supersedes` to the MCP `store` tool (lands
--      in this PR — wires through to INSERTs below this migration), or
--   2. the curator agent (Phase 3) starts inferring `cites` edges from
--      memory body text (deferred).

CREATE TABLE IF NOT EXISTS devbrain.memory_dependencies (
    id              BIGSERIAL PRIMARY KEY,
    from_memory_id  UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    to_memory_id    UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    edge_type       TEXT NOT NULL CHECK (edge_type IN ('cites', 'depends_on', 'supersedes', 'contradicts')),
        -- 'cites'        → narrative reference, weakest signal
        -- 'depends_on'   → invalidating to_memory should re-evaluate from_memory
        -- 'supersedes'   → from_memory replaces to_memory (terminal)
        -- 'contradicts'  → surfaced as an integrity issue
    confidence      REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT,                       -- 'curator' / 'agent' / 'manual' / 'migration:014'
    metadata        JSONB,
    -- Self-loops are nonsense; the trinity (from, to, type) must be unique so
    -- repeated curator passes are idempotent.
    CONSTRAINT chk_no_self_loop CHECK (from_memory_id <> to_memory_id),
    CONSTRAINT uq_memory_dep_triplet UNIQUE (from_memory_id, to_memory_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_dep_from
    ON devbrain.memory_dependencies (from_memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_dep_to
    ON devbrain.memory_dependencies (to_memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_dep_type
    ON devbrain.memory_dependencies (edge_type);

COMMENT ON TABLE devbrain.memory_dependencies IS
    'Phase 3 / Atlas Step 1. Typed edges between memory rows. Drives the curator agent''s cascade re-evaluation when a memory is superseded. See docs/plans/2026-04-29-phase-3-discipline-layer.md.';

-- ─────────────────────────────────────────────────────────────────────────────
-- Backfill from legacy decisions.superseded_by
-- ─────────────────────────────────────────────────────────────────────────────
-- decisions.superseded_by points FROM the old (superseded) row TO the new
-- (superseding) row. The new edge_type='supersedes' edge is from NEW to OLD
-- (matches the design: from_memory replaces to_memory).
--
-- Decisions land in devbrain.memory via Phase 2 dual-writes with
-- memory.provenance_id = decisions.id and memory.kind = 'decision'. Use that
-- to walk both endpoints onto memory rows.
--
-- ON CONFLICT DO NOTHING because uq_memory_dep_triplet makes this idempotent
-- on re-runs of the migration in dev.

INSERT INTO devbrain.memory_dependencies (
    from_memory_id, to_memory_id, edge_type, confidence, created_by, metadata
)
SELECT
    new_m.id,
    old_m.id,
    'supersedes',
    1.0,
    'migration:014',
    jsonb_build_object(
        'legacy_old_decision_id', old_d.id,
        'legacy_new_decision_id', new_d.id
    )
FROM devbrain.decisions old_d
JOIN devbrain.decisions new_d ON old_d.superseded_by = new_d.id
JOIN devbrain.memory new_m
    ON new_m.provenance_id = new_d.id AND new_m.kind = 'decision'
JOIN devbrain.memory old_m
    ON old_m.provenance_id = old_d.id AND old_m.kind = 'decision'
WHERE old_d.superseded_by IS NOT NULL
  AND new_m.id <> old_m.id
ON CONFLICT (from_memory_id, to_memory_id, edge_type) DO NOTHING;

-- Track this migration in schema_migrations (009 introduced the tracker).
INSERT INTO devbrain.schema_migrations (filename, applied_at)
VALUES ('014_memory_dependencies.sql', now())
ON CONFLICT (filename) DO NOTHING;

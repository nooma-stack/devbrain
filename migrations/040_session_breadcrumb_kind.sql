-- ─────────────────────────────────────────────────────────────────────────────
-- 040: Add 'session_breadcrumb' to memory.kind CHECK constraint
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Powers the new `breadcrumb` MCP tool — a mid-session progress marker
-- the agent emits at meaningful milestones in long sessions (a
-- decision made, a problem solved, a direction change). Multiple
-- breadcrumbs sharing the same `provenance_id` (the conversation's
-- UUID, generated once at session start) chain into a narrative for
-- the final end_session summary.
--
-- Schema notes:
--   * The new kind goes through the same INSERT path as session_summary.
--   * tier='memory' (not 'lesson') — breadcrumbs aren't promoted atoms.
--   * Idempotency is NOT enforced via a unique index. Multiple
--     breadcrumbs at the same (provenance_id, title) are valid (a dev
--     can revisit the same topic with new insight). The seq column
--     (stored in applies_when JSONB) preserves order; the partial
--     index below speeds up "all breadcrumbs for this conversation".
--
-- Idempotent — wrapped in BEGIN/COMMIT for atomic apply under CI.

BEGIN;

-- Replace the kind check.
ALTER TABLE devbrain.memory DROP CONSTRAINT IF EXISTS memory_kind_check;
ALTER TABLE devbrain.memory ADD CONSTRAINT memory_kind_check
    CHECK (kind = ANY (ARRAY[
        'chunk',
        'decision',
        'pattern',
        'issue',
        'session_summary',
        'session_breadcrumb'
    ]));

-- Recency-by-conversation lookup for the breadcrumb chain query.
CREATE INDEX IF NOT EXISTS idx_memory_breadcrumb_chain
    ON devbrain.memory (provenance_id, created_at)
    WHERE kind = 'session_breadcrumb'
      AND archived_at IS NULL;

COMMENT ON INDEX devbrain.idx_memory_breadcrumb_chain IS
    'Migration 040: speeds up "show me the breadcrumb chain for '
    'conversation X" queries used by deep_search + the final '
    'end_session summary aggregation.';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('040_session_breadcrumb_kind.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

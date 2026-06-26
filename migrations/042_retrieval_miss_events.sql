-- ─────────────────────────────────────────────────────────────────────────────
-- 042: Retrieval-miss tracking
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Records when a deep_search returns nothing useful, so we stop dropping
-- the single most valuable recall-quality signal on the floor. Pairs with
-- the weekly decay audit (043): query-time misses + temporal drift give two
-- independent receipts on retrieval health.
--
-- Multi-dev: every row is scoped by project_id and attributed by dev_id
-- (from DEVBRAIN_DEV_ID on the per-dev MCP process) + conversation_uuid
-- (the start_session chain). Append-only inserts — no write contention on
-- the shared Mac Studio DB.
--
-- Failure-class vocabulary is borrowed verbatim from engram-go so miss data
-- stays comparable across systems.
--
-- top_score is the key field Open Brain lacks: it splits "genuine gap"
-- (low/zero top_score → missing_content, write the memory) from "precision
-- failure" (high top_score, wrong answer → stale_ranking, a ranking problem).
--
-- Idempotent (CREATE … IF NOT EXISTS). Re-running is a no-op.
--
-- Usage:
--   docker exec -i devbrain-db psql -U devbrain -d devbrain < migrations/042_retrieval_miss_events.sql

BEGIN;

CREATE TABLE IF NOT EXISTS devbrain.retrieval_miss_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    project_id          UUID NOT NULL REFERENCES devbrain.projects(id),
    -- dev_id matches devbrain.devs.dev_id (VARCHAR(255)); nullable for
    -- non-attributed callers. Sourced from DEVBRAIN_DEV_ID.
    dev_id              VARCHAR(255) REFERENCES devbrain.devs(dev_id),
    -- Chains to the start_session / breadcrumb conversation UUID so a miss
    -- can be traced back to the session that hit it. No FK — the chain is a
    -- loose pointer, same convention as memory.provenance_id.
    conversation_uuid   UUID,
    query               TEXT NOT NULL,
    -- Re-embedded query (snowflake-arctic-embed2, 1024d) so repeated misses
    -- on the same topic cluster — surfaces systemic gaps, not one-offs.
    query_embedding     vector(1024),
    filter_source_types TEXT[],
    result_count        INTEGER NOT NULL DEFAULT 0,
    -- Best cosine the failed search achieved. Distinguishes a genuine
    -- content gap (low/zero) from a ranking/precision failure (high).
    top_score           NUMERIC,
    failure_class       TEXT NOT NULL CHECK (failure_class IN (
                          'vocabulary_mismatch',
                          'aggregation_failure',
                          'stale_ranking',
                          'missing_content',
                          'scope_mismatch',
                          'other')),
    notes               TEXT,
    -- dev_id of the human, or 'claude-code' when the agent self-logs.
    logged_by           VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS idx_miss_project_class
    ON devbrain.retrieval_miss_events (project_id, failure_class);

CREATE INDEX IF NOT EXISTS idx_miss_created
    ON devbrain.retrieval_miss_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_miss_dev
    ON devbrain.retrieval_miss_events (dev_id)
    WHERE dev_id IS NOT NULL;

-- Semantic clustering of repeated misses. lists=50 is sized for a table
-- that grows far slower than devbrain.memory.
CREATE INDEX IF NOT EXISTS idx_miss_embedding
    ON devbrain.retrieval_miss_events
    USING ivfflat (query_embedding vector_cosine_ops) WITH (lists = 50);

COMMENT ON TABLE devbrain.retrieval_miss_events IS
    'Migration 042: query-time recall failures, project-scoped + dev-attributed. '
    'Powers the log_miss MCP tool and per-project/per-dev miss-distribution rollups. '
    'Pairs with the 043 decay audit.';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('042_retrieval_miss_events.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

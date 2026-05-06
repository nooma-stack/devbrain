-- Migration 029: cognify_spend_log + cognify_spend_daily view
-- ============================================================================
-- Tracks per-call LLM cost estimates for all cognify passes and eval agents.
-- Project-scoped (P3): all rows carry project_id; cross-project aggregation
-- is intentionally NOT supported in the daily view.
--
-- PHI constraint: NO raw session content stored here — only token counts,
-- model name, pass name, project_id, and timestamps.
--
-- Costs are ESTIMATES based on hardcoded Anthropic list prices (see
-- factory/observability/pricing.py). Not integrated with the billing API.
--
-- Phase 6 design: docs/plans/2026-05-05-phase-6-cognify-memify-design.md §6

CREATE TABLE IF NOT EXISTS devbrain.cognify_spend_log (
    id                  BIGSERIAL PRIMARY KEY,
    project_id          UUID REFERENCES devbrain.projects(id),
    pass_name           TEXT NOT NULL,          -- 'extract', 'edges', 'eval_security', …
    model               TEXT NOT NULL,          -- 'claude-sonnet-4-6', …
    input_tokens        INTEGER NOT NULL,
    output_tokens       INTEGER NOT NULL,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_write_tokens  INTEGER DEFAULT 0,
    cost_usd            NUMERIC(10, 6) NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cognify_spend_log_project_day
    ON devbrain.cognify_spend_log (project_id, occurred_at DESC);

-- ── Daily aggregation view ────────────────────────────────────────────────────
-- Sums spend by (project_id, calendar day, model).
-- project_id is preserved — never aggregated away (P3 isolation).
CREATE OR REPLACE VIEW devbrain.cognify_spend_daily AS
SELECT
    project_id,
    date_trunc('day', occurred_at)::date  AS day,
    model,
    sum(input_tokens)                     AS input_tokens,
    sum(output_tokens)                    AS output_tokens,
    sum(cache_read_tokens)                AS cache_read_tokens,
    sum(cache_write_tokens)               AS cache_write_tokens,
    sum(cost_usd)                         AS cost_usd,
    count(*)                              AS call_count
FROM devbrain.cognify_spend_log
GROUP BY project_id, date_trunc('day', occurred_at)::date, model
ORDER BY day DESC, project_id, model;

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('029_cognify_spend_log.sql')
ON CONFLICT (filename) DO NOTHING;

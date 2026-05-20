-- ─────────────────────────────────────────────────────────────────────────────
-- 041: Add summary_source to devbrain.raw_sessions
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Powers cognify_resummarize — the deferred pass that upgrades Ollama
-- summaries to Sonnet when an end_session call never landed (so we
-- only pay the Sonnet cost for the sessions where the agent didn't
-- already provide a curated summary).
--
-- Values used:
--   * NULL              — pre-041 row OR not yet summarized
--   * 'ollama'          — initial Ollama qwen2.5:7b summary from the
--                          ingest pipeline (free, lower quality)
--   * 'sonnet'          — cognify_resummarize upgrade (~$0.01–0.02/session)
--   * 'opus'            — future Opus upgrade pass for the same path
--
-- Partial index on (summary_source, updated_at) restricts to rows that
-- have a summary text — the resummarize discovery query joins back to
-- chunks anyway, so this index keeps it cheap without indexing every
-- raw_session.
--
-- Idempotent.

BEGIN;

ALTER TABLE devbrain.raw_sessions
    ADD COLUMN IF NOT EXISTS summary_source VARCHAR(32);

COMMENT ON COLUMN devbrain.raw_sessions.summary_source IS
    'Which summarizer produced raw_sessions.summary: ollama (initial '
    'ingest, free, qwen2.5:7b), sonnet (cognify_resummarize upgrade), '
    'or opus (future). NULL for pre-041 rows. Used by '
    'cognify_resummarize to find Ollama summaries that should be '
    'upgraded after the conversation settled with no end_session call.';

CREATE INDEX IF NOT EXISTS idx_raw_sessions_summary_source
    ON devbrain.raw_sessions (summary_source)
    WHERE summary IS NOT NULL;

COMMENT ON INDEX devbrain.idx_raw_sessions_summary_source IS
    'Migration 041: speeds up cognify_resummarize discovery — "find '
    'raw_sessions whose summary_source is ollama or NULL and is ready '
    'for Sonnet upgrade."';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('041_raw_session_summary_source.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

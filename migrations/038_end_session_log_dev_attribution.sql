-- ─────────────────────────────────────────────────────────────────────────────
-- 038: Add dev_id + cli columns to devbrain.end_session_log
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Issue #135 — the TUI dashboard needs to render "recently-ended sessions"
-- with the dev + CLI behind each session. raw_sessions doesn't carry that
-- context today, and adding it there would require backfilling every prior
-- session. end_session_log is the lighter-touch surface: it's appended at
-- end_session time, where the MCP server already knows the dev_id (via
-- the per-dev profile HOME) and the calling CLI (passed in via
-- DEVBRAIN_MCP_CLI env var on the per-CLI MCP config).
--
-- Both columns are nullable. Pre-038 rows have NULL — the dashboard
-- panel renders those as "—" so the gap is visible rather than silently
-- inferred. The MCP server-driven writes (post-deploy) populate them.
--
-- Idempotent: wrapped in DO so re-applying is a no-op.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'devbrain'
          AND table_name = 'end_session_log'
          AND column_name = 'dev_id'
    ) THEN
        ALTER TABLE devbrain.end_session_log
            ADD COLUMN dev_id VARCHAR(64);
        COMMENT ON COLUMN devbrain.end_session_log.dev_id IS
            'Dev who owned the session. NULL for pre-038 rows and for '
            'local (non-SSH) Patrick sessions where no per-dev profile applies.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'devbrain'
          AND table_name = 'end_session_log'
          AND column_name = 'cli'
    ) THEN
        ALTER TABLE devbrain.end_session_log
            ADD COLUMN cli VARCHAR(32);
        COMMENT ON COLUMN devbrain.end_session_log.cli IS
            'Calling CLI: claude | codex | gemini | claude-desktop | unknown. '
            'Sourced from DEVBRAIN_MCP_CLI env var on the per-CLI MCP server '
            'config. NULL for pre-038 rows.';
    END IF;
END $$;

-- Recency-by-CLI lookups for the dashboard panel.
CREATE INDEX IF NOT EXISTS idx_end_session_log_applied_at_desc
    ON devbrain.end_session_log (applied_at DESC);

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('038_end_session_log_dev_attribution.sql')
ON CONFLICT (filename) DO NOTHING;

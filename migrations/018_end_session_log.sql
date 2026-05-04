-- Atlas Step 5e — end_session() idempotency log
-- ============================================================================
--
-- Adds devbrain.end_session_log: records every (session_id, payload_hash) the
-- MCP server's end_session tool has applied. Used by
-- factory.curator.end_session.end_session_idempotent_handler to make repeat
-- calls observe identical state without re-applying side effects.
--
-- session_id is the calling agent's logical session identifier (string —
-- shape is agent-defined). payload_hash is sha256 of the canonical-JSON
-- payload. The pair is the natural idempotency key: same session_id +
-- different payload means the agent submitted a corrected judgment, which
-- the handler treats as a NEW application (different hash = new row, new
-- side-effects). Same session_id + same payload returns the prior result
-- verbatim.
--
-- result is JSONB so we can return arbitrary handler bookkeeping
-- (cascades_drained count, drain_error, etc.) without altering the schema.

CREATE TABLE IF NOT EXISTS devbrain.end_session_log (
    session_id    TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    project_id    UUID NOT NULL REFERENCES devbrain.projects(id),
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result        JSONB,
    PRIMARY KEY (session_id, payload_hash)
);

-- For the dashboard / debug view: "show me all end_session calls in this
-- project, newest first."
CREATE INDEX IF NOT EXISTS idx_end_session_log_project_recent
    ON devbrain.end_session_log (project_id, applied_at DESC);

-- Track this migration in schema_migrations (009 introduced the tracker).
INSERT INTO devbrain.schema_migrations (filename, applied_at)
VALUES ('018_end_session_log.sql', now())
ON CONFLICT (filename) DO NOTHING;

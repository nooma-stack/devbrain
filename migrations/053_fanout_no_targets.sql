-- ─────────────────────────────────────────────────────────────────────────────
-- 053: Fan-out sessions with no target
-- ─────────────────────────────────────────────────────────────────────────────
--
-- One row per session the fan-out classifier judged to have no target
-- project. A fan-out run writes memory rows only for sessions that do have
-- one, and discovery finds "sessions without a fan-out row", so without this
-- table every such session went back to the LLM on every run.
--
--   * chunk_count   — the session's chunks when it was judged. Discovery picks
--                     the session up again once it has more, so a session that
--                     kept growing after the judgment gets a fresh one.
--   * classified_at — when it was judged.
--
-- To have sessions judged again (say, after adding a project to the taxonomy),
-- delete their rows.
--
-- New table only: no change to raw_sessions or memory. Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS devbrain.fanout_no_targets (
    session_id    UUID PRIMARY KEY
                  REFERENCES devbrain.raw_sessions(id) ON DELETE CASCADE,
    chunk_count   INT NOT NULL,
    classified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE devbrain.fanout_no_targets IS
    'Sessions the fan-out classifier found no target for, so discovery skips '
    'them until they grow. See factory/cognify/fanout.py.';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('053_fanout_no_targets.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

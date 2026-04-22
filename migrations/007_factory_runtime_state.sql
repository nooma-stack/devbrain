-- factory_runtime_state: singleton-style key/value store for factory-wide
-- runtime flags. Used by factory/readiness.py to persist a "not_ready"
-- signal when the pre- or post-job readiness check detects that the
-- working tree, git HEAD, or lock table is in a contaminated state
-- that cannot be auto-repaired.
--
-- Current known keys:
--   not_ready — reasons jsonb contains a list of {kind, message, details}
--               describing each issue. Presence of this row is the signal;
--               absence means the factory is ready.
--
-- Designed as a generic key-value table so future flags (e.g. "paused",
-- "draining") can reuse it without a schema change.

CREATE TABLE IF NOT EXISTS devbrain.factory_runtime_state (
    key         TEXT PRIMARY KEY,
    reasons     JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  TEXT
);

COMMENT ON TABLE devbrain.factory_runtime_state IS
  'Singleton-style factory-wide runtime flags (current key: not_ready).';

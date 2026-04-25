-- Migration 009: schema_migrations tracking table.
-- ============================================================================
--
-- Records which numbered SQL files in migrations/ have already been applied
-- to this database, so the `devbrain migrate` runner can skip them on the
-- next install/upgrade. Filename is the primary key (the runner sorts files
-- lexically and applies any not yet recorded). The checksum column is
-- intentionally nullable for now — a future change can populate it for
-- drift detection without a schema bump.
--
-- The trailing INSERT block backfills 001-008 with ON CONFLICT DO NOTHING so
-- existing installs (where these migrations were already applied manually
-- before the runner existed) don't re-run them when `devbrain migrate`
-- first launches.
--
-- Idempotent: re-running this file is a no-op.
--
-- Usage:
--   docker exec -i devbrain-db psql -U devbrain -d devbrain < migrations/009_schema_migrations.sql

CREATE TABLE IF NOT EXISTS devbrain.schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum    TEXT
);

COMMENT ON TABLE devbrain.schema_migrations IS
  'Tracks which migrations/*.sql files have been applied. Managed by `devbrain migrate`.';

-- Backfill prior migrations as already-applied so the runner doesn't replay
-- them on the first run after upgrade.
INSERT INTO devbrain.schema_migrations (filename) VALUES
    ('001_initial_schema.sql'),
    ('002_create_vector_indexes.sql'),
    ('003_cleanup_agent.sql'),
    ('004_file_registry.sql'),
    ('005_notifications.sql'),
    ('006_blocked_state.sql'),
    ('007_factory_runtime_state.sql'),
    ('008_artifact_warning_count.sql'),
    ('009_schema_migrations.sql')
ON CONFLICT (filename) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- 039: Phase 8 foundation — per-dev home projects + fan-out attribution
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Three independent pieces:
--
--   1. Backfill one `home-<dev_id>` project per existing dev. Future devs
--      get theirs lazily on first cognify_fanout encounter (handled in
--      Python). Replaces the deferred "inbox" concept from PR #121 §6.
--
--   2. Add `memory.fanout_source_session_id` (FK → raw_sessions). This
--      column is the discriminator that lets the dashboard panel +
--      `derived_from` graph queries tell fan-out rows apart from
--      agent-volunteered session_summary rows. NULL for non-fan-out rows.
--
--   3. Partial unique index on (fanout_source_session_id, project_id)
--      WHERE kind='session_summary' AND tier='memory' AND archived_at
--      IS NULL — the idempotency seal. Replaying cognify_fanout on the
--      same source session is a no-op against the same target.
--
-- All three pieces are idempotent (IF NOT EXISTS / WHERE NOT EXISTS).
-- Wrapped in an explicit transaction so the migration applies atomically
-- under CI's psql -f runner (which uses implicit-transaction-per-statement
-- if not wrapped — see incident notes on migration 034).
--
-- Roll-back: drop the unique index + the column. The home projects can be
-- archived (status='archived') without harm; they hold only fan-out rows
-- that are themselves archive-on-demand.

BEGIN;

-- 1. Per-dev home projects ---------------------------------------------------
--
-- One row per dev, slug='home-<dev_id>'. Idempotent via WHERE NOT EXISTS.
-- description prefix makes these searchable in admin UIs.
INSERT INTO devbrain.projects (id, slug, name, description, created_at)
SELECT
    gen_random_uuid(),
    'home-' || d.dev_id,
    COALESCE(d.full_name, d.dev_id) || ' — home',
    'Auto-managed catch-all for ' || d.dev_id || ' sessions with no other ' ||
        'clear project signal. Created by migration 039 (Phase 8 fan-out).',
    now()
FROM devbrain.devs d
WHERE NOT EXISTS (
    SELECT 1 FROM devbrain.projects p WHERE p.slug = 'home-' || d.dev_id
);

-- Orphan home for pre-PR-146 sessions with no dev_id attribution.
INSERT INTO devbrain.projects (id, slug, name, description, created_at)
SELECT
    gen_random_uuid(),
    'home-orphan',
    'Orphan sessions (pre-PR-146)',
    'Auto-managed catch-all for sessions with no dev_id attribution. ' ||
        'Created by migration 039 (Phase 8 fan-out). Stops accumulating ' ||
        'once every adapter attaches dev_id at ingest time.',
    now()
WHERE NOT EXISTS (
    SELECT 1 FROM devbrain.projects WHERE slug = 'home-orphan'
);


-- 2. Fan-out attribution column ----------------------------------------------
ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS fanout_source_session_id UUID
    REFERENCES devbrain.raw_sessions(id);

COMMENT ON COLUMN devbrain.memory.fanout_source_session_id IS
    'Set on Phase 8 fan-out rows: the raw_session whose content was '
    'classified into this project. NULL for non-fan-out memory rows. '
    'Powers the (fanout_source_session_id, project_id) idempotency '
    'index — re-running cognify_fanout on the same source is a no-op.';


-- 3. Fan-out idempotency seal -----------------------------------------------
--
-- Partial unique covers exactly: "this fan-out has already written an
-- active session_summary into this project for this source." Re-runs
-- collapse to ON CONFLICT DO NOTHING in the writer.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_fanout_unique
    ON devbrain.memory (fanout_source_session_id, project_id)
    WHERE kind = 'session_summary'
      AND tier = 'memory'
      AND archived_at IS NULL
      AND fanout_source_session_id IS NOT NULL;

COMMENT ON INDEX devbrain.idx_memory_fanout_unique IS
    'Migration 039: idempotency seal for cognify_fanout. One active '
    'session_summary row per (source_session, target_project). Combined '
    'with classifier checkpoint per session, re-running cognify_fanout '
    'is safe at any partial-failure point.';


INSERT INTO devbrain.schema_migrations (filename)
VALUES ('039_phase8_home_projects_and_fanout_attribution.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

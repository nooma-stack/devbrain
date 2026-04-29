-- DevBrain Project Port Registry
-- ===============================
-- Adds project lifecycle state + per-project port assignments.
--
-- Replaces the previous "edit a YAML file" convention (~/dev-port-registry.yml)
-- with a queryable table that the factory + AI agents can read and mutate via
-- the new `devbrain create-project` / `archive-project` / `ports` commands.
--
-- Design notes:
-- - Project status is one of {active, inactive, archived, experimental} per
--   the convention in /Users/patrickkelly/Nooma-Stack/50Tel PBX/docs/local-dev-port-registry.md.
-- - Ports are reserved for the project even when status=inactive. Only
--   archived ports can be reclaimed by another project, and only with
--   explicit confirmation.
-- - Port assignments support ranges (e.g., RTP media: 20000-20100) via the
--   port_start + port_end pair (port_end == port_start for a single port).
-- - Overlap-on-host detection is enforced at the application layer for v1.
--   A future migration can layer a btree_gist EXCLUDE constraint on top.

BEGIN;

-- ─── Projects: lifecycle + team metadata ─────────────────────────────────────

ALTER TABLE devbrain.projects
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive', 'archived', 'experimental')),
    ADD COLUMN IF NOT EXISTS team VARCHAR(100),
    ADD COLUMN IF NOT EXISTS compose_project VARCHAR(100),
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_projects_status ON devbrain.projects(status);
CREATE INDEX IF NOT EXISTS ix_projects_team ON devbrain.projects(team) WHERE team IS NOT NULL;

-- ─── Port assignments ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS devbrain.port_assignments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES devbrain.projects(id) ON DELETE RESTRICT,
    host        VARCHAR(255) NOT NULL DEFAULT 'localhost',
    purpose     VARCHAR(100) NOT NULL,
    port_start  INTEGER NOT NULL CHECK (port_start BETWEEN 1 AND 65535),
    port_end    INTEGER NOT NULL CHECK (port_end BETWEEN 1 AND 65535),
    notes       TEXT,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- archived_at: when the port was last released (e.g., the project was
    -- archived AND the assignment explicitly torn down by `reclaim-port`).
    -- For projects in `inactive` state, archived_at stays NULL — the port
    -- is still reserved.
    archived_at TIMESTAMPTZ,
    CHECK (port_end >= port_start),
    UNIQUE (project_id, purpose)
);

CREATE INDEX IF NOT EXISTS ix_port_assignments_project ON devbrain.port_assignments(project_id);
CREATE INDEX IF NOT EXISTS ix_port_assignments_host ON devbrain.port_assignments(host);
CREATE INDEX IF NOT EXISTS ix_port_assignments_active ON devbrain.port_assignments(host, port_start, port_end)
    WHERE archived_at IS NULL;

-- ─── Migration tracking ──────────────────────────────────────────────────────

INSERT INTO devbrain.schema_migrations (filename, applied_at)
VALUES ('012_port_registry.sql', now())
ON CONFLICT (filename) DO NOTHING;

COMMIT;

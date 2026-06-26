-- ─────────────────────────────────────────────────────────────────────────────
-- 043: Recall decay audit — canonical queries + drift snapshots
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Weekly regression test on retrieval quality. A per-project set of "golden"
-- queries whose top results should NOT drift week to week is replayed through
-- the real deep_search ranking path (runDeepSearchCore — shared with the MCP
-- tool so the audit can't measure a fiction). Each run snapshots the top
-- memory_ids + cosine scores per query and diffs against the prior snapshot.
--
-- Why tables, not committed JSON (the Open Brain design): on a shared
-- multi-dev Mac Studio, git-committed snapshots are a merge-race generator.
-- The DB is the source of truth; canonical queries live here so any dev can
-- add a golden question without a commit.
--
-- Concurrency: UNIQUE (project_id, run_date) is the idempotency seal
-- (migration-039 pattern). The runner additionally takes
-- pg_advisory_xact_lock(hashtext('recall_audit:'||project_id)) so two
-- devs/cron firing the same project audit on the same day is a no-op, not a
-- double-write.
--
-- ivfflat caveat (documented for the runner, not enforced here): devbrain.memory
-- uses an approximate ivfflat index (010, lists=100). The runner SHOULD
-- `SET LOCAL ivfflat.probes = <high>` (or force exact scan) so the audit
-- isolates genuine content/embedding drift from ANN probe jitter.
--
-- Idempotent (CREATE … IF NOT EXISTS). Re-running is a no-op.
--
-- Usage:
--   docker exec -i devbrain-db psql -U devbrain -d devbrain < migrations/043_recall_audit.sql

BEGIN;

-- Golden queries whose top results should stay stable as the bank grows.
CREATE TABLE IF NOT EXISTS devbrain.recall_canonical_queries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES devbrain.projects(id),
    query       TEXT NOT NULL,
    hint        TEXT,
    -- What this query is supposed to surface, in prose, for the human
    -- reading a drift alert. e.g. "must surface the billing-codes SoT".
    note        TEXT,
    active      BOOLEAN NOT NULL DEFAULT true,
    created_by  VARCHAR(255) REFERENCES devbrain.devs(dev_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_canonical_project_active
    ON devbrain.recall_canonical_queries (project_id)
    WHERE active;

-- One snapshot per project per run-date.
CREATE TABLE IF NOT EXISTS devbrain.recall_audit_snapshots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES devbrain.projects(id),
    run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_date        DATE NOT NULL,
    -- host / dev that triggered the run (audit trail; the audit itself is
    -- a system-level metric, not per-dev).
    run_by          VARCHAR(255),
    query_set_size  INTEGER NOT NULL,
    -- [{query, top_ids[], scores[], result_count, top_score, duration_ms}]
    results         JSONB NOT NULL,
    -- vs prior snapshot:
    -- [{query, jaccard, mean_score_delta, added[], removed[]}]
    -- jaccard = top-id set overlap (which memories drifted out);
    -- mean_score_delta = confidence drift even when ids are unchanged
    -- (the signal Open Brain's id-only Jaccard misses).
    drift           JSONB,
    CONSTRAINT uq_audit_per_day UNIQUE (project_id, run_date)
);

CREATE INDEX IF NOT EXISTS idx_audit_project_date
    ON devbrain.recall_audit_snapshots (project_id, run_date DESC);

COMMENT ON TABLE devbrain.recall_canonical_queries IS
    'Migration 043: per-project golden queries for the recall decay audit. '
    'Shared/multi-dev: any dev adds a query via row insert, no git commit.';

COMMENT ON TABLE devbrain.recall_audit_snapshots IS
    'Migration 043: weekly recall-drift snapshots. UNIQUE(project_id,run_date) '
    'is the idempotency seal; runner holds a per-project advisory lock. Drift '
    'beyond threshold fires devbrain_notify rather than failing a CI build.';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('043_recall_audit.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

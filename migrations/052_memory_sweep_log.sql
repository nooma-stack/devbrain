-- ─────────────────────────────────────────────────────────────────────────────
-- 052: Outdated-memory sweep log
-- ─────────────────────────────────────────────────────────────────────────────
--
-- One row per (older, newer) atom pair judged by the cognify `sweep` pass
-- (factory/cognify/sweep.py). Code pairs each atom with OLDER live atoms of
-- the same project above a cosine threshold; the deployment's local model
-- says whether the newer one replaces, contradicts, refines or does not
-- relate to the older one.
--
--   * verdict       — the model's answer, or 'error' when the call failed.
--   * mode          — 'shadow' (logged only) or 'on' (high-confidence
--                     replaces/contradicts written as edges).
--   * applied       — true when an edge was written for this pair.
--   * edge_type     — the edge written, newer -> older: 'supersedes' for
--                     replaces, 'contradicts' for contradicts.
--   * review_status — 'pending' for applied contradictions.
--
-- Not memory_relation_log (048): there a row marks its memory_id as checked,
-- so the write-time relation sweep would skip every newer atom judged here,
-- and its relation vocabulary has no 'replaces'.
--
-- A pair is judged once per mode (unique index; failed calls excluded so they
-- are retried). Switching shadow -> on re-judges pairs in 'on' mode.
--
-- No memory text is stored here (memory content may contain PHI); only ids,
-- scores and model metadata. The sweep never deletes or archives memory.
--
-- New table only: no change to devbrain.memory, no rewrite, no index build
-- on existing large tables. Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS devbrain.memory_sweep_log (
    id               BIGSERIAL PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    project_id       UUID NOT NULL REFERENCES devbrain.projects(id),
    older_memory_id  UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    newer_memory_id  UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    similarity       REAL NOT NULL,
    verdict          TEXT NOT NULL CHECK (verdict IN
                        ('replaces', 'contradicts', 'refines', 'unrelated', 'error')),
    confidence       REAL,
    mode             TEXT NOT NULL CHECK (mode IN ('shadow', 'on')),
    applied          BOOLEAN NOT NULL DEFAULT false,
    edge_type        TEXT CHECK (edge_type IN ('supersedes', 'contradicts')),
    model            TEXT,
    latency_ms       INTEGER,
    error            TEXT,
    review_status    TEXT CHECK (review_status IN ('pending', 'resolved', 'dismissed')),
    CONSTRAINT chk_memory_sweep_distinct CHECK (older_memory_id <> newer_memory_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_sweep_pair
    ON devbrain.memory_sweep_log (older_memory_id, newer_memory_id, mode)
 WHERE verdict <> 'error';
CREATE INDEX IF NOT EXISTS idx_memory_sweep_newer
    ON devbrain.memory_sweep_log (newer_memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_sweep_project
    ON devbrain.memory_sweep_log (project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_sweep_review
    ON devbrain.memory_sweep_log (project_id, created_at)
 WHERE review_status = 'pending';

COMMENT ON TABLE devbrain.memory_sweep_log IS
    'Outdated-memory sweep verdicts (replaces/contradicts/refines/unrelated) '
    'for (older, newer) atom pairs. See factory/cognify/sweep.py. No memory text.';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('052_memory_sweep_log.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

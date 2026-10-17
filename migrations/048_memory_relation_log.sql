-- ─────────────────────────────────────────────────────────────────────────────
-- 048: Write-time relation check log
-- ─────────────────────────────────────────────────────────────────────────────
--
-- One row per write-time relation check (factory/cognify/relate.py): when a
-- decision/pattern/issue atom is written, its nearest same-project atoms are
-- classified by the local model as same / refines / contradicts / new.
--
--   * memory_id          — the checked row. NULL when a "same" verdict
--                          prevented the write (the extract path never
--                          inserted it); matched_memory_id then names the
--                          existing row that was reinforced instead.
--   * matched_memory_id  — the existing atom the verdict refers to.
--   * relation           — applied verdict, or 'error' when the model call
--                          failed (the write then went ahead as a plain
--                          write). model_relation keeps the raw answer.
--   * mode               — 'shadow' (logged only) or 'on' (applied).
--   * applied            — true when the verdict changed the graph or the
--                          row (edge written, row archived/skipped).
--   * review_status      — 'pending' for contradictions, so an operator can
--                          confirm the supersedes edge. Resolve with
--                          UPDATE … SET review_status = 'resolved'.
--
-- The presence of a row also marks a memory as checked, so the async sweep
-- never re-classifies it.
--
-- No memory text is stored here (memory content may contain PHI); only ids,
-- scores and model metadata.
--
-- New table only: no change to devbrain.memory, no rewrite, no index build
-- on existing large tables. Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS devbrain.memory_relation_log (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    project_id        UUID NOT NULL REFERENCES devbrain.projects(id),
    memory_id         UUID REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    matched_memory_id UUID REFERENCES devbrain.memory(id) ON DELETE SET NULL,
    relation          TEXT NOT NULL CHECK (relation IN
                        ('same', 'refines', 'contradicts', 'new', 'error')),
    -- The model's raw answer before the confidence / similarity guards
    -- (e.g. a low-similarity "contradicts" is applied as 'new').
    model_relation    TEXT,
    similarity        REAL,
    confidence        REAL,
    candidates        SMALLINT NOT NULL DEFAULT 0,
    mode              TEXT NOT NULL CHECK (mode IN ('shadow', 'on')),
    source            TEXT NOT NULL,
    -- Free-form pointer to the write's origin (e.g. the source session id
    -- for extracted atoms). Not a FK.
    source_ref        TEXT,
    applied           BOOLEAN NOT NULL DEFAULT false,
    model             TEXT,
    latency_ms        INTEGER,
    error             TEXT,
    review_status     TEXT CHECK (review_status IN ('pending', 'resolved', 'dismissed'))
);

CREATE INDEX IF NOT EXISTS idx_memory_relation_log_memory
    ON devbrain.memory_relation_log (memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_relation_log_matched
    ON devbrain.memory_relation_log (matched_memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_relation_log_review
    ON devbrain.memory_relation_log (project_id, created_at)
 WHERE review_status = 'pending';

COMMENT ON TABLE devbrain.memory_relation_log IS
    'Write-time relation checks (same/refines/contradicts/new) for atoms. '
    'See factory/cognify/relate.py. No memory text.';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('048_memory_relation_log.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

-- ─────────────────────────────────────────────────────────────────────────────
-- 015: memory_ledger — hash-chained audit ledger for devbrain.memory writes
-- ─────────────────────────────────────────────────────────────────────────────
-- Phase 3 / Atlas Step 2 (see docs/plans/2026-04-29-phase-3-discipline-layer.md
-- §3.2 and PR #67). Adds:
--   • devbrain.memory_ledger — append-only audit table with SHA-256 row chain.
--     One row per write to devbrain.memory.
--   • Trigger function devbrain._memory_ledger_record() — computes prev_hash +
--     payload_hash + row_hash and inserts the audit row.
--   • AFTER triggers on memory for INSERT/UPDATE/DELETE.
--   • devbrain.verify_chain(start_seq, end_seq) — re-walks the chain and
--     surfaces the first divergence. Used by `devbrain audit verify` (Step 3).
--
-- Tamper model: an attacker who modifies a past memory_ledger row breaks the
-- hash chain at that row's seq AND every subsequent row that references it
-- via prev_hash. verify_chain() finds the first break.
--
-- Cost: ~150 bytes per memory write. At 60k current memory rows × ~3 writes
-- each ≈ 180k ledger rows ≈ 30 MB. Negligible.
--
-- Race-safety: pg_advisory_xact_lock() inside the trigger forces serialization
-- of concurrent ledger writes so prev_hash reads/writes are consistent. Adds
-- micro-latency under contention but guarantees chain integrity.
--
-- Note: ledger does NOT store the payload contents — only the SHA-256 hash
-- of the canonicalized JSONB. Auditors verify by re-canonicalizing the live
-- memory row and comparing. This is a tamper detector, not a duplicate store.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS devbrain.memory_ledger (
    seq             BIGSERIAL PRIMARY KEY,
    -- Not a foreign key: ledger must survive memory row deletion
    -- (whether soft-archive or accidental hard delete).
    memory_id       UUID NOT NULL,
    operation       TEXT NOT NULL CHECK (operation IN ('create', 'update', 'archive', 'restore', 'delete')),
    actor           TEXT NOT NULL,                   -- dev_id, 'curator', 'system', or current_user
    project_slug    TEXT NOT NULL,
    payload_hash    BYTEA NOT NULL,                  -- sha256(to_jsonb(memory_row)::text)
    prev_hash       BYTEA,                           -- previous row's row_hash; NULL for seq=1
    row_hash        BYTEA NOT NULL,                  -- sha256(seq | memory_id | op | actor | project | payload_hash | prev_hash)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_ledger_memory_id
    ON devbrain.memory_ledger (memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_ledger_project_created
    ON devbrain.memory_ledger (project_slug, created_at);

COMMENT ON TABLE devbrain.memory_ledger IS
    'Phase 3 / Atlas Step 2. Hash-chained append-only audit log of every devbrain.memory write. See docs/plans/2026-04-29-phase-3-discipline-layer.md §3.2.';

-- ─────────────────────────────────────────────────────────────────────────────
-- Trigger function: records every memory mutation with a fresh chain link
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION devbrain._memory_ledger_record()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    op            TEXT;
    payload_text  TEXT;
    payload_h     BYTEA;
    prev_h        BYTEA;
    next_seq      BIGINT;
    cur_row_hash  BYTEA;
    actor_name    TEXT;
    proj_id       UUID;
    proj_slug     TEXT;
    target_id     UUID;
BEGIN
    -- Resolve operation
    IF TG_OP = 'INSERT' THEN
        op := 'create';
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.archived_at IS NULL AND NEW.archived_at IS NOT NULL THEN
            op := 'archive';
        ELSIF OLD.archived_at IS NOT NULL AND NEW.archived_at IS NULL THEN
            op := 'restore';
        ELSE
            op := 'update';
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        op := 'delete';
    ELSE
        -- Defensive: TRUNCATE etc.; do nothing rather than corrupt the chain.
        RETURN NULL;
    END IF;

    -- Canonical payload (JSONB sorts keys deterministically, so this is
    -- stable across rewrites of the same row).
    IF TG_OP = 'DELETE' THEN
        payload_text := to_jsonb(OLD)::text;
        target_id    := OLD.id;
        proj_id      := OLD.project_id;
    ELSE
        payload_text := to_jsonb(NEW)::text;
        target_id    := NEW.id;
        proj_id      := NEW.project_id;
    END IF;

    payload_h := digest(payload_text, 'sha256');

    -- Project slug (denormalized into the ledger for human-readable audits).
    SELECT slug INTO proj_slug FROM devbrain.projects WHERE id = proj_id;
    IF proj_slug IS NULL THEN
        proj_slug := 'unknown';
    END IF;

    -- Actor: prefer an app-set GUC ('devbrain.actor'); fall back to current_user.
    actor_name := COALESCE(current_setting('devbrain.actor', true), current_user);

    -- Serialize concurrent ledger writes so prev_hash is consistent.
    -- hashtext is deterministic for the same string, so all writers share
    -- the same lock id.
    PERFORM pg_advisory_xact_lock(hashtext('devbrain.memory_ledger'));

    -- Reserve next seq + read prev_hash atomically (within the lock).
    next_seq := nextval(pg_get_serial_sequence('devbrain.memory_ledger', 'seq'));
    SELECT row_hash INTO prev_h
    FROM devbrain.memory_ledger
    ORDER BY seq DESC
    LIMIT 1;

    -- Compose the row hash. Use hex encoding for hashes inside the text
    -- input so the format is stable and verifiable from outside Postgres.
    cur_row_hash := digest(
        next_seq::text
            || '|' || target_id::text
            || '|' || op
            || '|' || actor_name
            || '|' || proj_slug
            || '|' || encode(payload_h, 'hex')
            || '|' || COALESCE(encode(prev_h, 'hex'), ''),
        'sha256'
    );

    INSERT INTO devbrain.memory_ledger (
        seq, memory_id, operation, actor, project_slug,
        payload_hash, prev_hash, row_hash
    ) VALUES (
        next_seq, target_id, op, actor_name, proj_slug,
        payload_h, prev_h, cur_row_hash
    );

    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS trg_memory_ledger_insert ON devbrain.memory;
CREATE TRIGGER trg_memory_ledger_insert
    AFTER INSERT ON devbrain.memory
    FOR EACH ROW EXECUTE FUNCTION devbrain._memory_ledger_record();

DROP TRIGGER IF EXISTS trg_memory_ledger_update ON devbrain.memory;
CREATE TRIGGER trg_memory_ledger_update
    AFTER UPDATE ON devbrain.memory
    FOR EACH ROW EXECUTE FUNCTION devbrain._memory_ledger_record();

DROP TRIGGER IF EXISTS trg_memory_ledger_delete ON devbrain.memory;
CREATE TRIGGER trg_memory_ledger_delete
    AFTER DELETE ON devbrain.memory
    FOR EACH ROW EXECUTE FUNCTION devbrain._memory_ledger_record();

-- ─────────────────────────────────────────────────────────────────────────────
-- verify_chain: re-walks the chain, returns the first divergent seq
-- ─────────────────────────────────────────────────────────────────────────────
-- Returns one row per discovered break:
--   (broken_at_seq, expected_hash, actual_hash, reason)
-- Empty result set means the requested range is intact.
--
-- broken_at_seq = 0 with reason='gap' means start_seq references a missing
-- predecessor (start_seq > 1 but seq start_seq-1 doesn't exist).
CREATE OR REPLACE FUNCTION devbrain.verify_chain(
    start_seq BIGINT DEFAULT 1,
    end_seq   BIGINT DEFAULT NULL
)
RETURNS TABLE(
    broken_at_seq BIGINT,
    expected_hash TEXT,
    actual_hash   TEXT,
    reason        TEXT
)
LANGUAGE plpgsql
AS $$
DECLARE
    prev_row_hash BYTEA;
    cur RECORD;
    expected BYTEA;
BEGIN
    IF start_seq > 1 THEN
        SELECT row_hash INTO prev_row_hash
        FROM devbrain.memory_ledger
        WHERE seq = start_seq - 1;
        IF NOT FOUND THEN
            RETURN QUERY SELECT 0::BIGINT, NULL::TEXT, NULL::TEXT, 'gap-before-start'::TEXT;
            RETURN;
        END IF;
    END IF;

    FOR cur IN
        SELECT seq, memory_id, operation, actor, project_slug,
               payload_hash, prev_hash, row_hash
        FROM devbrain.memory_ledger
        WHERE seq >= start_seq
          AND (end_seq IS NULL OR seq <= end_seq)
        ORDER BY seq
    LOOP
        IF cur.prev_hash IS DISTINCT FROM prev_row_hash THEN
            RETURN QUERY SELECT
                cur.seq,
                COALESCE(encode(prev_row_hash, 'hex'), ''),
                COALESCE(encode(cur.prev_hash, 'hex'), ''),
                'prev_hash-mismatch';
            RETURN;
        END IF;

        expected := digest(
            cur.seq::text
                || '|' || cur.memory_id::text
                || '|' || cur.operation
                || '|' || cur.actor
                || '|' || cur.project_slug
                || '|' || encode(cur.payload_hash, 'hex')
                || '|' || COALESCE(encode(cur.prev_hash, 'hex'), ''),
            'sha256'
        );

        IF expected IS DISTINCT FROM cur.row_hash THEN
            RETURN QUERY SELECT
                cur.seq,
                encode(expected, 'hex'),
                encode(cur.row_hash, 'hex'),
                'row_hash-mismatch';
            RETURN;
        END IF;

        prev_row_hash := cur.row_hash;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION devbrain.verify_chain IS
    'Walks devbrain.memory_ledger from start_seq..end_seq, returning the first chain break (or empty if intact). See docs/plans/2026-04-29-phase-3-discipline-layer.md §3.2.';

-- Track this migration in schema_migrations (012/013 convention).
INSERT INTO devbrain.schema_migrations (filename, applied_at)
VALUES ('015_memory_ledger.sql', now())
ON CONFLICT (filename) DO NOTHING;

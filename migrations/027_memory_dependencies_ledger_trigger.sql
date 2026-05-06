-- ═══════════════════════════════════════════════════════════════════════════
-- 027: memory_dependencies ledger trigger — edge-level audit trail
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Phase 5.x (worker multi-hop + edge audit). Adds an AFTER trigger on
-- devbrain.memory_dependencies so that every edge INSERT/UPDATE/DELETE writes
-- a corresponding row to devbrain.memory_ledger for full audit coverage.
--
-- Design rationale
-- ────────────────
-- Migration 015 wired ledger triggers on devbrain.memory (the node table).
-- But memory_dependencies (the edge table) was never covered — an edge
-- insert or delete left no audit trail. Phase 5 graph semantics make edges
-- first-class objects (six typed edges, confidence, metadata) so edge
-- mutations must be audited too.
--
-- The ledger row for an edge event:
--   memory_id    = from_memory_id   — the dependent side; primary subject
--   operation    = 'edge_added' | 'edge_updated' | 'edge_removed'
--   actor        = devbrain.actor GUC or current_user
--   project_slug = resolved from from_memory_id → memory → projects
--   payload_hash = sha256(JSONB of edge fields: edge_type, to_memory_id,
--                          confidence, metadata)
--   prev_hash / row_hash = same hash-chain mechanics as migration 015
--
-- The operation column CHECK constraint is extended to include the three new
-- edge event values so both memory-row events and edge events share the same
-- ledger table.
--
-- Determinism guarantee: the trigger always uses to_jsonb(ROW(...)::text) with
-- a fixed column order so the same edge state always produces the same
-- payload_hash. This is equivalent to the memory trigger's `to_jsonb(NEW)`.
--
-- Cost: one additional ledger row per edge mutation. Edge mutations are less
-- frequent than memory writes; negligible overhead.
--
-- Note: ledger rows for edge events share the same hash chain as memory-row
-- ledger rows (seq is global to the table). This means verify_chain() will
-- cross-validate both kinds of events together — intentional, since the goal
-- is a single tamper-evident log of all devbrain mutations.
--
-- ═══════════════════════════════════════════════════════════════════════════

-- ── 1. Extend the operation CHECK constraint to include edge event types ──────
ALTER TABLE devbrain.memory_ledger
    DROP CONSTRAINT IF EXISTS memory_ledger_operation_check;

ALTER TABLE devbrain.memory_ledger
    ADD CONSTRAINT memory_ledger_operation_check
    CHECK (operation IN (
        'create', 'update', 'archive', 'restore', 'delete',
        'edge_added', 'edge_updated', 'edge_removed'
    ));

-- ── 2. Trigger function for memory_dependencies edge events ──────────────────
CREATE OR REPLACE FUNCTION devbrain._memory_dependencies_ledger_record()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    op            TEXT;
    edge_row      RECORD;
    payload_text  TEXT;
    payload_h     BYTEA;
    prev_h        BYTEA;
    next_seq      BIGINT;
    cur_row_hash  BYTEA;
    actor_name    TEXT;
    proj_id       UUID;
    proj_slug     TEXT;
    target_id     UUID;  -- from_memory_id (dependent side, primary subject)
BEGIN
    IF TG_OP = 'INSERT' THEN
        op       := 'edge_added';
        edge_row := NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        op       := 'edge_updated';
        edge_row := NEW;
    ELSIF TG_OP = 'DELETE' THEN
        op       := 'edge_removed';
        edge_row := OLD;
    ELSE
        -- Defensive: TRUNCATE etc.; do nothing rather than corrupt the chain.
        RETURN NULL;
    END IF;

    -- Primary subject = from_memory_id (the dependent side whose state is
    -- implicated by the edge). This matches the audit spec in migration 027.
    target_id := edge_row.from_memory_id;

    -- Canonical payload: stable JSONB of the edge fields that matter for audit.
    -- Fixed key order via jsonb_build_object so hash is deterministic regardless
    -- of column addition order in future migrations.
    payload_text := jsonb_build_object(
        'edge_type',     edge_row.edge_type,
        'to_memory_id',  edge_row.to_memory_id,
        'confidence',    edge_row.confidence,
        'metadata',      edge_row.metadata
    )::text;

    payload_h := digest(payload_text, 'sha256');

    -- Resolve project_slug from the from_memory row. May be NULL if the
    -- memory was hard-deleted (edge DELETE fires before cascade from memory).
    SELECT p.slug INTO proj_slug
    FROM devbrain.memory m
    JOIN devbrain.projects p ON p.id = m.project_id
    WHERE m.id = target_id;

    IF proj_slug IS NULL THEN
        proj_slug := 'unknown';
    END IF;

    -- Actor: prefer app-set GUC; fall back to current_user.
    actor_name := COALESCE(current_setting('devbrain.actor', true), current_user);

    -- Serialize concurrent ledger writes (same lock as migration 015 so
    -- memory-row events and edge events are serialized against each other,
    -- preserving a single coherent hash chain).
    PERFORM pg_advisory_xact_lock(hashtext('devbrain.memory_ledger'));

    -- Reserve next seq + read prev_hash atomically (within the lock).
    next_seq := nextval(pg_get_serial_sequence('devbrain.memory_ledger', 'seq'));
    SELECT row_hash INTO prev_h
    FROM devbrain.memory_ledger
    ORDER BY seq DESC
    LIMIT 1;

    -- Compose the row hash using the same formula as migration 015 so
    -- verify_chain() validates both kinds of events uniformly.
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

-- ── 3. Attach triggers to memory_dependencies ────────────────────────────────
DROP TRIGGER IF EXISTS trg_memory_dep_ledger_insert ON devbrain.memory_dependencies;
CREATE TRIGGER trg_memory_dep_ledger_insert
    AFTER INSERT ON devbrain.memory_dependencies
    FOR EACH ROW EXECUTE FUNCTION devbrain._memory_dependencies_ledger_record();

DROP TRIGGER IF EXISTS trg_memory_dep_ledger_update ON devbrain.memory_dependencies;
CREATE TRIGGER trg_memory_dep_ledger_update
    AFTER UPDATE ON devbrain.memory_dependencies
    FOR EACH ROW EXECUTE FUNCTION devbrain._memory_dependencies_ledger_record();

DROP TRIGGER IF EXISTS trg_memory_dep_ledger_delete ON devbrain.memory_dependencies;
CREATE TRIGGER trg_memory_dep_ledger_delete
    AFTER DELETE ON devbrain.memory_dependencies
    FOR EACH ROW EXECUTE FUNCTION devbrain._memory_dependencies_ledger_record();

-- ── 4. Track this migration ───────────────────────────────────────────────────
INSERT INTO devbrain.schema_migrations (filename, applied_at)
VALUES ('027_memory_dependencies_ledger_trigger.sql', now())
ON CONFLICT (filename) DO NOTHING;

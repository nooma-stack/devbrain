-- Migration 034: prune orphan ledger rows + rebuild the hash chain
-- ============================================================================
-- After migration 033 deleted ~14M polluted memory rows, the audit ledger
-- still held one entry per delete (and one per the matching prior
-- create/archive) for those rows. ~14.22M of 14.30M ledger rows became
-- orphans referencing memory_id values that no longer exist. Only ~74K
-- entries still reference live memory.
--
-- The ledger is a tamper-evident hash chain (see migration 015):
--   row_hash = sha256(seq | memory_id | op | actor | project | hex(payload_hash) | hex(prev_hash))
-- so we cannot simply DELETE the orphans without breaking the chain at
-- every prune site. This migration does both:
--
--   1. DELETE ledger rows whose memory_id no longer exists in
--      devbrain.memory (the orphans).
--   2. Walk the remaining rows in seq order and recompute each row's
--      prev_hash + row_hash so verify_chain() passes end-to-end.
--      The first remaining row gets prev_hash = NULL (genesis), and
--      each subsequent row's prev_hash = previous remaining row's
--      new row_hash.
--
-- Trade-off: we lose the original hashes for the orphan-bracketed
-- segments. An attacker who had the pre-rebuild ledger could detect
-- that we rewrote the chain. For laptop devbrain, that's an acceptable
-- trade — the orphan entries were audit logs of a pollution bug, not
-- of any real activity. The 74K live-row entries are preserved with
-- their original semantic content (memory_id, operation, payload_hash);
-- only the chain-pointer fields (prev_hash, row_hash) change.
--
-- Lock: we hold ACCESS EXCLUSIVE on both memory and memory_ledger for
-- the duration so concurrent writes can't slip in mid-rebuild. The
-- pollution-cleanup window is the natural time to do this.
--
-- Idempotency: re-running on a clean chain is a no-op. The DELETE
-- affects zero rows when there are no orphans, and the rebuild
-- rewrites the same hashes (deterministic from same inputs).
--
-- Reclaim: combined with a VACUUM FULL on memory_ledger afterward
-- (cannot run inside a migration transaction), expect to reclaim
-- approximately 3 GB on the laptop devbrain.
--
-- Explicit BEGIN/COMMIT: LOCK TABLE only works inside a transaction
-- block. The schema runner (factory/schema_migrate.py) wraps each file
-- in psycopg2's transaction; but CI's bootstrap uses plain
-- `psql -f file.sql` which runs each statement in its own implicit
-- transaction. Wrapping the body explicitly makes the migration work
-- in both contexts.

BEGIN;

LOCK TABLE devbrain.memory IN ACCESS EXCLUSIVE MODE;
LOCK TABLE devbrain.memory_ledger IN ACCESS EXCLUSIVE MODE;

-- Step 1: drop orphan ledger rows.
DELETE FROM devbrain.memory_ledger l
WHERE NOT EXISTS (
  SELECT 1 FROM devbrain.memory m WHERE m.id = l.memory_id
);

-- Step 2: rebuild the hash chain over the remaining rows.
-- The chain reconnects in seq order; the first remaining row becomes
-- the new genesis (prev_hash = NULL).
DO $$
DECLARE
    cur       RECORD;
    prev_h    BYTEA := NULL;
    new_hash  BYTEA;
BEGIN
    FOR cur IN
        SELECT seq, memory_id, operation, actor, project_slug, payload_hash
        FROM devbrain.memory_ledger
        ORDER BY seq
    LOOP
        new_hash := digest(
            cur.seq::text
                || '|' || cur.memory_id::text
                || '|' || cur.operation
                || '|' || cur.actor
                || '|' || cur.project_slug
                || '|' || encode(cur.payload_hash, 'hex')
                || '|' || COALESCE(encode(prev_h, 'hex'), ''),
            'sha256'
        );

        UPDATE devbrain.memory_ledger
        SET prev_hash = prev_h,
            row_hash  = new_hash
        WHERE seq = cur.seq;

        prev_h := new_hash;
    END LOOP;
END $$;

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('034_ledger_orphan_prune_and_rechain.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

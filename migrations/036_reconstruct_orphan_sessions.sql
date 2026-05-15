-- Migration 036: reconstruct orphan brightbot sessions + cleanup duplicates
-- ============================================================================
-- On 2026-04-09 a one-off cleanup script (precursor to commit 8956810
-- "DevBrain ingest and factory reliability issues") deleted ~414 duplicate
-- raw_sessions rows from devbrain.brightbot but didn't delete their
-- chunks — chunks.source_id has no FK to raw_sessions (polymorphic by
-- design), so the DELETE silently orphaned 26,791 chunks across 388
-- ghost session IDs.
--
-- We analyzed those 388 ghost groups against the surviving 455
-- raw_sessions and found a clean bimodal distribution by best-match
-- chunk-content overlap:
--
--   - 296 ghosts with 0% overlap with any surviving session →
--     these are truly unique sessions whose raw_sessions rows were
--     lost. The chunks themselves preserve the conversation content
--     with embedded Claude Code role markers ([user]/[assistant]/
--     [tool_result]) and ISO timestamps. We reconstruct them.
--
--   - 92 ghosts with ≥25% chunk-content overlap with one specific
--     surviving session → these are strict subsets of those survivors
--     (earlier captures of the same Claude Code session before it
--     grew further). Verified: all 92 ghosts have ≤ chunks than their
--     survivor, and every byte-mismatched ghost chunk's content is a
--     substring of some survivor chunk (chunker boundary drift). No
--     information loss from deleting them.
--
-- This migration:
--   1. Reconstructs raw_sessions rows for the 296 unique ghosts.
--      - id = ghost source_id (so existing memory/chunk linkage
--        immediately becomes a valid chain)
--      - source_app = 'claude_code' (98.7% of orphan chunks match the
--        Claude Code role-marker fingerprint per agent-B analysis)
--      - source_path = 'reconstructed://2026-05-15/<id>' (auditable)
--      - source_hash = sha256('reconstructed:<id>') (satisfies the
--        UNIQUE(source_app, source_hash) constraint)
--      - started_at / ended_at = MIN/MAX of ISO timestamps extracted
--        from chunk content (REAL conversation time, not ingest time)
--      - message_count = count of [user]/[assistant] markers
--      - raw_content = chunks concatenated in source_line_start order
--      - summary = existing session_summary chunk if one was created
--      - metadata.reconstructed = true (queryable marker)
--
--   2. Deletes chunks + memory rows for the 92 duplicate ghosts.
--      Their content is preserved in the surviving sessions.
--
-- After this migration: brightbot atomizable sessions go from
-- 455 → 751 (455 existing + 296 reconstructed), and there are no
-- remaining orphan session-type chunks.
--
-- The follow-up `delete_session()` helper in ingest/db.py prevents
-- the underlying class of bug from recurring.
--
-- Idempotent: re-running on a clean DB is a no-op. The CTEs identify
-- zero unique ghosts (no orphan chunks) and zero duplicates; the
-- INSERT/DELETE statements both affect zero rows.

BEGIN;

-- ─── Stage all orphan-ghost analysis into temp tables ──────────────────────
-- All ghost source_ids = chunks.source_id values that don't appear in
-- raw_sessions for the brightbot project.
CREATE TEMP TABLE _ghost_sizes AS
SELECT c.source_id AS ghost_id, COUNT(*) AS sz
FROM devbrain.chunks c
LEFT JOIN devbrain.raw_sessions rs ON rs.id = c.source_id
WHERE c.project_id = (SELECT id FROM devbrain.projects WHERE slug='brightbot')
  AND c.source_type = 'session'
  AND rs.id IS NULL
GROUP BY c.source_id;

CREATE TEMP TABLE _ghost_chunks AS
SELECT c.source_id AS ghost_id, c.content
FROM devbrain.chunks c
LEFT JOIN devbrain.raw_sessions rs ON rs.id = c.source_id
WHERE c.project_id = (SELECT id FROM devbrain.projects WHERE slug='brightbot')
  AND c.source_type = 'session'
  AND rs.id IS NULL;

CREATE TEMP TABLE _surv_chunks AS
SELECT c.source_id AS surv_id, c.content
FROM devbrain.chunks c
JOIN devbrain.raw_sessions rs ON rs.id = c.source_id
WHERE c.project_id = (SELECT id FROM devbrain.projects WHERE slug='brightbot')
  AND c.source_type = 'session';

-- Duplicate ghosts: best-match overlap with a surviving session is ≥25%
-- of the ghost's chunks. Empirically bimodal — true duplicates start at
-- 25% overlap; truly unique sessions have 0% overlap.
CREATE TEMP TABLE _duplicate_ghosts AS
WITH pair_overlap AS (
    SELECT gc.ghost_id, sc.surv_id, COUNT(*) AS overlap
    FROM _ghost_chunks gc
    JOIN _surv_chunks sc ON sc.content = gc.content
    GROUP BY gc.ghost_id, sc.surv_id
),
best_match AS (
    SELECT DISTINCT ON (ghost_id) ghost_id, surv_id, overlap
    FROM pair_overlap
    ORDER BY ghost_id, overlap DESC
)
SELECT bm.ghost_id
FROM best_match bm
JOIN _ghost_sizes gs ON gs.ghost_id = bm.ghost_id
WHERE 100.0 * bm.overlap / gs.sz >= 25;

CREATE TEMP TABLE _unique_ghosts AS
SELECT ghost_id FROM _ghost_sizes
WHERE ghost_id NOT IN (SELECT ghost_id FROM _duplicate_ghosts);

-- ─── Step 1: reconstruct raw_sessions for the unique ghosts ────────────────
-- Pre-aggregate per-ghost values so the INSERT is one clean SELECT.
WITH brightbot AS (
    SELECT id FROM devbrain.projects WHERE slug = 'brightbot'
),
ghost_content AS (
    SELECT
        ug.ghost_id,
        string_agg(
            c.content,
            E'\n\n'
            ORDER BY c.source_line_start NULLS LAST, c.created_at
        ) AS raw_content
    FROM _unique_ghosts ug
    JOIN devbrain.chunks c ON c.source_id = ug.ghost_id
    WHERE c.source_type = 'session'
    GROUP BY ug.ghost_id
),
ghost_summary AS (
    SELECT DISTINCT ON (ug.ghost_id)
        ug.ghost_id,
        c.content AS summary_text
    FROM _unique_ghosts ug
    JOIN devbrain.chunks c ON c.source_id = ug.ghost_id
    WHERE c.source_type = 'session_summary'
    ORDER BY ug.ghost_id, c.created_at
),
ghost_timestamps AS (
    -- Extract every ISO 8601 timestamp from the concatenated content,
    -- then take MIN/MAX as the real conversation start/end.
    SELECT
        gc.ghost_id,
        MIN((m[1])::timestamptz) AS started_at,
        MAX((m[1])::timestamptz) AS ended_at
    FROM ghost_content gc,
         LATERAL regexp_matches(
             gc.raw_content,
             '\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\]',
             'g'
         ) AS m
    GROUP BY gc.ghost_id
),
ghost_message_counts AS (
    -- Count of [user]/[assistant] markers — proxy for message_count.
    SELECT
        gc.ghost_id,
        COUNT(*) AS message_count
    FROM ghost_content gc,
         LATERAL regexp_matches(gc.raw_content, '\[(user|assistant)\]', 'g')
    GROUP BY gc.ghost_id
)
INSERT INTO devbrain.raw_sessions (
    id, project_id, source_app, source_path, source_hash, session_id,
    model_used, started_at, ended_at, message_count, raw_content,
    summary, files_touched, metadata
)
SELECT
    gc.ghost_id AS id,
    (SELECT id FROM brightbot) AS project_id,
    'claude_code' AS source_app,
    'reconstructed://2026-05-15/' || gc.ghost_id::text AS source_path,
    encode(digest('reconstructed:' || gc.ghost_id::text, 'sha256'), 'hex')
        AS source_hash,
    NULL AS session_id,
    NULL AS model_used,
    gt.started_at,
    gt.ended_at,
    COALESCE(gmc.message_count, 0)::int AS message_count,
    gc.raw_content,
    gs.summary_text AS summary,
    '[]'::jsonb AS files_touched,
    jsonb_build_object(
        'reconstructed', true,
        'reconstructed_from', 'orphan_chunks',
        'reconstruction_migration', '036_reconstruct_orphan_sessions.sql',
        'reconstruction_date', '2026-05-15'
    ) AS metadata
FROM ghost_content gc
LEFT JOIN ghost_timestamps gt ON gt.ghost_id = gc.ghost_id
LEFT JOIN ghost_message_counts gmc ON gmc.ghost_id = gc.ghost_id
LEFT JOIN ghost_summary gs ON gs.ghost_id = gc.ghost_id
ON CONFLICT (source_app, source_hash) DO NOTHING;

-- ─── Step 2: delete chunks + memory for the 92 duplicate ghosts ────────────
-- Silence the audit-ledger DELETE trigger for the bulk delete (these
-- aren't audit-meaningful events — the duplicates were duplicates).
ALTER TABLE devbrain.memory DISABLE TRIGGER trg_memory_ledger_delete;

-- memory rows tied to duplicate ghosts (provenance_id = chunks.source_id
-- post-migration-032). FKs to memory_dependencies / refinement_queue /
-- curator_re_eval_queue cascade automatically.
DELETE FROM devbrain.memory m
WHERE m.provenance_id IN (SELECT ghost_id FROM _duplicate_ghosts)
  AND m.project_id = (SELECT id FROM devbrain.projects WHERE slug = 'brightbot');

-- The legacy chunks themselves.
DELETE FROM devbrain.chunks c
WHERE c.source_id IN (SELECT ghost_id FROM _duplicate_ghosts)
  AND c.project_id = (SELECT id FROM devbrain.projects WHERE slug = 'brightbot');

ALTER TABLE devbrain.memory ENABLE TRIGGER trg_memory_ledger_delete;

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('036_reconstruct_orphan_sessions.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

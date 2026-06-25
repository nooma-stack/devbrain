-- ─────────────────────────────────────────────────────────────────────────────
-- 045: Chunk dual-write dedup — collapse re-ingested duplicate chunks
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Context (2026-06-25): BrightBrain's deep_search flagged a recency_warning
-- ("stale by 75+ days") on essentially every result. Root cause: the
-- legacy→memory dual-write (ingest/memory_writer.py) gave kind='chunk' no
-- ON CONFLICT clause — migration 037's idempotency only covered atom +
-- session_summary kinds — so every re-run of the chunk backfill re-inserted
-- every chunk. The BrightBot project had grown to 315,776 chunk rows for
-- only 58,628 distinct (provenance_id, content) chunks (one chunk had 824
-- copies; the June ingests were 97–98% duplicate). Those old duplicates
-- dominated deep_search's top-K and, being >30 days old, tripped the
-- topic-age-gap staleness trigger on every query.
--
-- This migration:
--   (1) repoints memory_dependencies edges off duplicate chunk rows onto
--       the surviving (earliest) row that shares the natural key, skipping
--       repoints that would violate uq_memory_dep_triplet (those duplicate
--       edges CASCADE-drop with their dup row in step 2);
--   (2) deletes the duplicate chunk rows, keeping the earliest per
--       (provenance_id, kind, md5(content)); and
--   (3) adds the partial unique index that makes the dual-write's new
--       ON CONFLICT (provenance_id, kind, md5(content)) idempotent —
--       mirroring idx_memory_session_summary_unique from 037.
--
-- Natural key rationale: provenance_id is the *source session* UUID
-- (migration 032), so a session holds many distinct chunks under one
-- provenance — the content hash is what identifies an individual chunk.
-- The key is provenance-scoped, so identical text from *different* sessions
-- is preserved (consistent with 044's divergent-provenance reasoning); only
-- a re-ingestion of the *same* session's *same* chunk collapses.
--
-- Idempotent: on a DB with no chunk dupes (fresh installs, the laptop
-- DevBrain) the repoint/delete touch zero rows and the index creates
-- cleanly.

-- (1) Repoint from-edges off dup chunks onto the surviving row.
WITH ranked AS (
    SELECT id,
           first_value(id) OVER w AS survivor_id,
           row_number()    OVER w AS rn
      FROM devbrain.memory
     WHERE kind = 'chunk' AND provenance_id IS NOT NULL
    WINDOW w AS (
        PARTITION BY provenance_id, md5(content)
        ORDER BY created_at ASC, id ASC
    )
),
dup_map AS (
    SELECT id AS dup_id, survivor_id FROM ranked WHERE rn > 1
)
UPDATE devbrain.memory_dependencies d
   SET from_memory_id = dm.survivor_id
  FROM dup_map dm
 WHERE d.from_memory_id = dm.dup_id
   AND NOT EXISTS (
        SELECT 1 FROM devbrain.memory_dependencies e
         WHERE e.from_memory_id = dm.survivor_id
           AND e.to_memory_id   = d.to_memory_id
           AND e.edge_type      = d.edge_type
   );

-- (1b) Repoint to-edges off dup chunks (none observed today, included so
--      the migration is correct regardless of edge direction).
WITH ranked AS (
    SELECT id,
           first_value(id) OVER w AS survivor_id,
           row_number()    OVER w AS rn
      FROM devbrain.memory
     WHERE kind = 'chunk' AND provenance_id IS NOT NULL
    WINDOW w AS (
        PARTITION BY provenance_id, md5(content)
        ORDER BY created_at ASC, id ASC
    )
),
dup_map AS (
    SELECT id AS dup_id, survivor_id FROM ranked WHERE rn > 1
)
UPDATE devbrain.memory_dependencies d
   SET to_memory_id = dm.survivor_id
  FROM dup_map dm
 WHERE d.to_memory_id = dm.dup_id
   AND NOT EXISTS (
        SELECT 1 FROM devbrain.memory_dependencies e
         WHERE e.to_memory_id   = dm.survivor_id
           AND e.from_memory_id = d.from_memory_id
           AND e.edge_type      = d.edge_type
   );

-- (2) Delete the duplicate chunk rows (keep earliest per natural key).
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY provenance_id, md5(content)
               ORDER BY created_at ASC, id ASC
           ) AS rn
      FROM devbrain.memory
     WHERE kind = 'chunk' AND provenance_id IS NOT NULL
)
DELETE FROM devbrain.memory m
 USING ranked r
 WHERE m.id = r.id AND r.rn > 1;

-- (3) Partial unique index — makes the chunk dual-write idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_chunk_dedup_unique
    ON devbrain.memory (provenance_id, kind, md5(content))
 WHERE kind = 'chunk' AND provenance_id IS NOT NULL;

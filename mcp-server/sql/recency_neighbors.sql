-- deep_search recency neighbors (recency.ts expandRecencyNeighbors): for
-- each primary, the overfetch ($3) nearest same-project rows created at
-- least $2 days later, kept when cosine >= $4; the caller takes the k most
-- recent per primary. Shared verbatim with ingest/recall_audit/recency.py.
-- No percent signs in this file (psycopg2).
--
-- Plan note: the planner misestimates per-project row counts and, left
-- alone, answers the LATERAL with an exact bitmap-scan + sort that reads
-- every newer row's embedding (~430 ms per primary on a 90K-row project).
-- With strategy 'hnsw' the caller runs this under SET LOCAL enable_sort =
-- off and a bounded hnsw.max_scan_tuples, so the HNSW index (iterative
-- scan) answers it instead.
WITH primaries AS (
    SELECT id, created_at, embedding, project_id
      FROM devbrain.memory
     WHERE id = ANY($1::uuid[])
       AND archived_at IS NULL
       AND embedding IS NOT NULL
)
SELECT
    p.id::text AS primary_id,
    n.id::text AS neighbor_id,
    n.kind     AS neighbor_kind,
    n.content  AS neighbor_content,
    n.created_at AS neighbor_created_at,
    1 - (n.embedding <=> p.embedding) AS similarity,
    EXTRACT(EPOCH FROM (n.created_at - p.created_at)) / 86400.0
        AS age_delta_days
  FROM primaries p
  CROSS JOIN LATERAL (
        SELECT m.id, m.kind, m.content, m.created_at, m.embedding
          FROM devbrain.memory m
         WHERE m.id <> p.id
           AND m.archived_at IS NULL
           AND m.embedding IS NOT NULL
           AND m.project_id = p.project_id
           AND m.created_at > p.created_at + ($2 || ' days')::interval
         ORDER BY m.embedding <=> p.embedding
         LIMIT $3
       ) n
 WHERE 1 - (n.embedding <=> p.embedding) >= $4
 ORDER BY p.id, n.created_at DESC

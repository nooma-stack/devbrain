-- deep_search near-duplicate collapse: similar pairs among a candidate set.
--
-- Single source of truth: mcp-server/src/index.ts (deep_search) and
-- ingest/recall_audit both run this exact text. No percent signs in this
-- file: the audit runs it through psycopg2.
--
-- Parameters
--   $1 uuid[]   candidate memory ids (the fused candidate pool, <= ~100)
--   $2 float8   cosine similarity threshold
--
-- Returns every unordered pair (a < b) with cosine >= $2, plus each
-- side's created_at (epoch seconds) for the newest-representative option. The candidates'
-- embeddings are detoasted once into a materialized CTE (l2_normalize
-- returns an in-memory copy; cosine is unchanged by normalization), so
-- the n^2/2 distance calls do not re-read TOAST.
WITH e AS MATERIALIZED (
  SELECT id, created_at, l2_normalize(embedding) AS v
  FROM devbrain.memory
  WHERE id = ANY($1::uuid[]) AND embedding IS NOT NULL
)
SELECT
  a.id::text AS a_id, b.id::text AS b_id, 1 - (a.v <=> b.v) AS similarity,
  extract(epoch FROM a.created_at)::float8 AS a_created,
  extract(epoch FROM b.created_at)::float8 AS b_created
FROM e a
JOIN e b ON a.id < b.id
WHERE 1 - (a.v <=> b.v) >= $2::float8

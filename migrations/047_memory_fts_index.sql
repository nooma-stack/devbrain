-- ─────────────────────────────────────────────────────────────────────────────
-- 047: Full-text (keyword) index for deep_search's hybrid retrieval
-- ─────────────────────────────────────────────────────────────────────────────
--
-- deep_search fuses vector search with a Postgres full-text keyword channel
-- (mcp-server/sql/keyword_search.sql, Reciprocal Rank Fusion in
-- mcp-server/src/hybrid.ts). That SQL searches exactly this expression, so
-- the planner can use the index; keep the two in sync.
--
-- * Expression index, not a generated column: no table rewrite on the
--   ~120K+ row memory table, and it can be built CONCURRENTLY.
-- * 'simple' config: no stemming, no stop words, so identifiers, file names
--   and error strings index as written. Measured equal to 'english' on the
--   recall audit; term frequency (from ANALYZE stats on this index) replaces
--   a stop-word list.
-- * Partial on archived_at IS NULL, like the HNSW index and deep_search.
--
-- Large installs: build it CONCURRENTLY first (a migration runs inside a
-- transaction, so it cannot), then run `devbrain migrate`; the CREATE below
-- is then a no-op:
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_memory_fts_simple
--     ON devbrain.memory
--     USING gin (to_tsvector('simple'::regconfig, coalesce(title, '') || ' ' || content))
--     WHERE archived_at IS NULL;
--
-- If a CONCURRENTLY build fails it leaves an INVALID index: DROP INDEX
-- CONCURRENTLY devbrain.idx_memory_fts_simple and build again. The keyword
-- SQL checks indisvalid and returns nothing until the index is valid.
--
-- ANALYZE collects most_common_elems for the index expression; the keyword
-- SQL reads them to drop query terms too common to discriminate. Without
-- the stats it still works, it just counts every term through the index.

CREATE INDEX IF NOT EXISTS idx_memory_fts_simple
    ON devbrain.memory
    USING gin (to_tsvector('simple'::regconfig, coalesce(title, '') || ' ' || content))
    WHERE archived_at IS NULL;

ANALYZE devbrain.memory;

-- ─────────────────────────────────────────────────────────────────────────────
-- 046: Switch the embedding ANN index from ivfflat to HNSW
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Context (2026-06-26): replace the ivfflat embedding index with HNSW.
-- Measured on the live BrightBrain data (65k embedded rows) both indexes
-- give identical retrieval quality — tie-robust distance-recall@10 = 1.000
-- at ivfflat probes=10 AND hnsw ef_search=40, both ~1ms p50. (An earlier
-- id-overlap metric mis-read tied/duplicate embeddings as recall misses;
-- it was a measurement artifact, NOT a real recall gap.) HNSW is chosen
-- for the marginal wins: ~half the on-disk size (337 MB vs 656 MB at this
-- row count) and no probes tuning to maintain — equal recall/latency.
--
-- Build notes:
--   * `max_parallel_maintenance_workers = 0` — pgvector's PARALLEL HNSW
--     build allocates a large shared-memory segment that overflows a
--     containerized Postgres's small /dev/shm (observed: "could not resize
--     shared memory segment ... No space left on device"). A single-threaded
--     build uses private backend memory instead. Harmless on bare-metal too.
--   * On the Studio the HNSW index was already built CONCURRENTLY (a
--     migration can't use CONCURRENTLY — it runs in a transaction), so the
--     CREATE here is a no-op there (IF NOT EXISTS) and only the ivfflat DROP
--     applies. On fresh/small installs (laptop, CI) this builds HNSW in the
--     migration transaction — fast at those row counts.
--   * The partial `WHERE archived_at IS NULL` matches deep_search's filter
--     (mirrors how the queries are written) so the index stays usable.

SET max_parallel_maintenance_workers = 0;
SET maintenance_work_mem = '2GB';

CREATE INDEX IF NOT EXISTS idx_memory_embedding_hnsw
    ON devbrain.memory USING hnsw (embedding vector_cosine_ops)
    WHERE archived_at IS NULL;

DROP INDEX IF EXISTS devbrain.idx_memory_embedding;

-- Run AFTER initial data load for optimal IVFFlat index quality.
-- IVFFlat needs existing data to build good cluster centroids.
--
-- Usage:
--   docker exec -i devbrain-db psql -U devbrain -d devbrain < migrations/002_create_vector_indexes.sql

CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON devbrain.chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_codebase_embedding ON devbrain.codebase_index
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

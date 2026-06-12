-- ─────────────────────────────────────────────────────────────────────────────
-- 044: Content-level dedup index for import-memory
-- ─────────────────────────────────────────────────────────────────────────────
--
-- import-memory's idempotency can no longer ride on unique indexes:
-- 037's per-kind partial indexes only cover three kinds (and only rows
-- with provenance_id), so re-importing the same export duplicated every
-- uncovered row (observed 2026-06-12: ~50k duplicate rows on a re-run).
-- The importer now anti-joins on a content-level natural key; this
-- non-unique expression index makes that NOT EXISTS probe an index
-- lookup instead of a per-row sequential scan.
--
-- Deliberately NOT unique: legitimate duplicate content can exist
-- (e.g. divergent-provenance rows predating this migration); the
-- importer's anti-join — not a constraint — enforces import dedup.

CREATE INDEX IF NOT EXISTS idx_memory_import_dedup
    ON devbrain.memory (project_id, kind, md5(content));

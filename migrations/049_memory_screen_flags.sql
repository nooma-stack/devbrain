-- ─────────────────────────────────────────────────────────────────────────────
-- 049: memory.screen_flags — prompt-injection screening marks
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Rule ids from ingest/screening.py for rows whose text looks like a prompt
-- injection: override, agent_directive, fake_tag, exfil, hidden_unicode,
-- obfuscated.
-- NULL means no flag (clean, or not screened yet); only flagged rows ever
-- get a value, so neither ingest nor the backfill
-- (`devbrain screen-memory --apply`) touches clean rows.
--
-- Flagged text is withheld from summary, extraction, fan-out and
-- re-summary prompts. deep_search can select the column to label results.
--
-- ADD COLUMN with no default is a catalog-only change in Postgres: no table
-- rewrite and no index build on the large memory table. Idempotent.

BEGIN;

ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS screen_flags TEXT[];

COMMENT ON COLUMN devbrain.memory.screen_flags IS
    'Prompt-injection screening rule ids (ingest/screening.py); NULL = no flag.';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('049_memory_screen_flags.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

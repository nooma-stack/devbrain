-- deep_search keyword channel: Postgres full-text search over devbrain.memory.
--
-- Single source of truth: mcp-server/src/index.ts (deep_search) and
-- ingest/recall_audit both run this exact text, so the audit measures what
-- ships. No percent signs in this file: the audit runs it through psycopg2.
--
-- Parameters
--   $1 text     raw search query
--   $2 float8   max document frequency ratio a query term may have (0..1)
--   $3 float8   row budget: max summed document count of the kept terms
--   $4 int      max rows returned
--   $5 text     query embedding as a pgvector literal (score = cosine)
--   $6 uuid     project id, or NULL for cross-project
--   $7 text[]   memory.kind filter, or NULL
--   $8 text[]   chunks.source_type filter for kind='chunk' rows, or NULL
--   $9 int      max query terms kept
--   $10 bool    identifier tokens only (snake_case, dotted, slashed,
--               CONSTANT_CASE, camelCase, with digits; not hyphenated
--               prose) instead of every token
--
-- How it ranks
--   Each whitespace token of the query becomes one unit: a phrase query of
--   its lexemes ('simple' config, so identifiers, file names and error
--   strings stay intact: work_item_open_ticket_uniq is the phrase
--   work <-> item <-> open <-> ticket <-> uniq). Document frequency per unit
--   comes from the index's ANALYZE statistics (most_common_elems) for a
--   single common lexeme; rare lexemes and phrases of them are counted
--   exactly through the index. Units too common to discriminate (df > $2) are
--   dropped -- this replaces a stop-word list and keeps the matched set
--   small: the rarest units are kept until their summed df reaches $3.
--   A row's score is the summed BM25 idf of the units it matches, built
--   from one index scan per kept unit (no per-row to_tsvector except the
--   GIN recheck of phrase units); ties go to the higher cosine similarity
--   among the top 4 x $4 rows.
--   MATERIALIZED CTEs keep each expensive step from being re-evaluated.
--
-- The whole statement is gated on idx_memory_fts_simple existing and being
-- valid (migration 047), so an install without the index returns no rows
-- instead of a sequential to_tsvector scan.
WITH
gate AS MATERIALIZED (
  SELECT EXISTS (
    SELECT 1 FROM pg_index
    WHERE indexrelid = to_regclass('devbrain.idx_memory_fts_simple') AND indisvalid
  ) AS ok
),
fts_stats AS MATERIALIZED (
  SELECT e.lex, e.freq
  FROM pg_stats s
  CROSS JOIN LATERAL unnest(s.most_common_elems::text::text[], s.most_common_elem_freqs) AS e(lex, freq)
  WHERE s.schemaname = 'devbrain' AND s.tablename = 'idx_memory_fts_simple' AND e.lex IS NOT NULL
),
corpus AS MATERIALIZED (
  SELECT greatest(coalesce(
    (SELECT reltuples FROM pg_class WHERE oid = to_regclass('devbrain.idx_memory_fts_simple')), 0), 1)::float8 AS n
),
tokens AS MATERIALIZED (
  SELECT t,
    (t ~ '[[:alnum:]][-_./:#@][[:alnum:]]'           -- snake_case, file.ext, a/b, host:port
     OR t ~ '[[:lower:]][[:upper:]]'                 -- camelCase
     OR t ~ '^[[:upper:][:digit:]_]{4,}$'            -- CONSTANT_CASE, ECONNREFUSED
     OR (t ~ '[[:digit:]]' AND char_length(t) >= 3)) -- 095, qwen3, v0.137.0
    AND t !~ '^[[:alpha:]]+(-[[:alpha:]]+)+$'        -- hyphenated prose: long-term, per-team
    AS ident
  FROM (
    SELECT regexp_replace(regexp_replace(raw, '[^[:alnum:]_]+$', ''), '^[^[:alnum:]_/.#~-]+', '') AS t
    FROM regexp_split_to_table(left($1::text, 2000), '\s+') AS raw
    LIMIT 64
  ) x
  WHERE t <> ''
),
units AS MATERIALIZED (
  SELECT DISTINCT phraseto_tsquery('simple', t) AS q, tsvector_to_array(to_tsvector('simple', t)) AS lexes
  FROM tokens, gate
  WHERE gate.ok AND (tokens.ident OR NOT $10::boolean)
),
unit_df AS MATERIALIZED (
  SELECT u.q,
    CASE WHEN mce.all_known
      AND (cardinality(u.lexes) = 1 OR mce.min_freq * c.n > 2 * $3::float8)
    THEN mce.min_freq * c.n
    ELSE (
      SELECT count(*) FROM devbrain.memory m2
      WHERE m2.archived_at IS NULL
        AND to_tsvector('simple'::regconfig, coalesce(m2.title, '') || ' ' || m2.content) @@ u.q
    )::float8
    END AS df
  FROM units u
  CROSS JOIN corpus c
  CROSS JOIN LATERAL (
    SELECT bool_and(st.freq IS NOT NULL) AS all_known, min(st.freq) AS min_freq
    FROM unnest(u.lexes) AS l
    LEFT JOIN fts_stats st ON st.lex = l
  ) mce
  WHERE numnode(u.q) > 0
),
kept AS MATERIALIZED (
  SELECT r.q, ln(1 + (c.n - r.df + 0.5) / (r.df + 0.5)) AS idf
  FROM (
    SELECT q, df,
      sum(df) OVER (ORDER BY df, q::text ROWS UNBOUNDED PRECEDING) AS cum_df,
      row_number() OVER (ORDER BY df, q::text) AS rn
    FROM unit_df
    WHERE df > 0
  ) r
  CROSS JOIN corpus c
  WHERE r.df <= $2::float8 * c.n AND r.cum_df <= $3::float8 AND r.rn <= $9::int
),
hits AS MATERIALIZED (
  SELECT h.id, sum(k.idf) AS keyword_score
  FROM kept k
  CROSS JOIN LATERAL (
    SELECT m.id FROM devbrain.memory m
    WHERE m.archived_at IS NULL
      AND ($6::uuid IS NULL OR m.project_id = $6::uuid)
      AND to_tsvector('simple'::regconfig, coalesce(m.title, '') || ' ' || m.content) @@ k.q
  ) h
  GROUP BY h.id
),
-- Filter and pre-cut on keyword score alone (newest first within a tie),
-- then compute cosine only for that short list: detoasting every hit's
-- 4 KB embedding was most of the statement's time.
pre AS (
  SELECT
    m.id AS memory_id,
    m.kind AS memory_kind,
    m.content,
    m.embedding,
    c.id AS legacy_chunk_id,
    c.source_type AS chunk_source_type,
    c.source_id AS chunk_source_id,
    c.source_line_start AS chunk_line_start,
    c.source_line_end AS chunk_line_end,
    p.slug AS project,
    hits.keyword_score
  FROM hits
  JOIN devbrain.memory m ON m.id = hits.id
  JOIN devbrain.projects p ON m.project_id = p.id
  LEFT JOIN devbrain.chunks c
    ON m.kind = 'chunk' AND c.id = m.provenance_id
    AND (c.source_type IS NULL OR c.source_type NOT IN
         ('decision','pattern','issue','session_summary'))
  WHERE m.embedding IS NOT NULL
    AND ($7::text[] IS NULL OR m.kind = ANY($7::text[]))
    AND ($8::text[] IS NULL OR m.kind <> 'chunk' OR c.source_type = ANY($8::text[]))
  ORDER BY hits.keyword_score DESC, m.created_at DESC
  LIMIT 4 * $4::int
)
SELECT
  memory_id, memory_kind, content,
  1 - (embedding <=> $5::vector) AS score,
  legacy_chunk_id, chunk_source_type, chunk_source_id, chunk_line_start,
  chunk_line_end, project, keyword_score
FROM pre
ORDER BY keyword_score DESC, score DESC
LIMIT $4::int

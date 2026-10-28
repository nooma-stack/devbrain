-- ─────────────────────────────────────────────────────────────────────────────
-- 050: Facts graph — entities, typed relations, aliases, and the walk
-- ─────────────────────────────────────────────────────────────────────────────
--
-- memory_dependencies links memories to memories. This adds a graph of
-- THINGS IN THE WORLD (hosts, repos, accounts, domains, folders ...) so a
-- question like "which GitHub account and brain go with ~/Trifekta?" is
-- answered by walking typed edges, not by searching text. Design follows
-- Glitch Cat Club's graph-memory-starter (MIT): content-addressed entities,
-- typed relations that carry their source, aliases for name variants, and
-- one recursive walk with no model call.
--
-- Written by the cognify `entities` pass (factory/cognify/entities.py),
-- read by devbrain.facts_walk() (the MCP `facts` tool, mcp-server/src/facts.ts).
--
-- Closed vocabulary (CHECK constraints below; keep it small):
--
--   Entity types
--     PROJECT  a product / workstream / business line (DevBrain, Granlee Ranch)
--     REPO     a git repository (Trifekta-AI/devplane)
--     HOST     a machine or VM (Mac Studio, SOHO VPS, the MacBook)
--     SERVICE  a running thing: app, daemon, container, database, MCP server,
--              LaunchAgent, deployment (opsdesk, devbrain-gateway)
--     ACCOUNT  a login/identity on a platform (GitHub account `trifekta`,
--              patrick@trifekta.ai, a GCP service account)
--     DOMAIN   a DNS name (opsdesk.trifekta.ai)
--     PERSON   a human (Patrick, Mark, Joao)
--     ORG      a company or GitHub org (Trifekta AI, Lighthouse Therapy)
--     TOOL     software used, not run by us (codex CLI, gh, Caddy)
--     PATH     a folder or file location (~/Trifekta, ~/.claude-trifekta)
--
--   Predicates (source --[predicate]--> target)
--     runs_on        SERVICE/TOOL/PROJECT -> HOST              (single-valued)
--     deploys_to     REPO/PROJECT/SERVICE -> HOST/SERVICE      (single-valued)
--     owned_by       anything -> PERSON/ORG/ACCOUNT            (single-valued)
--     uses_account   anything -> ACCOUNT
--     part_of        anything -> PROJECT/ORG/REPO/SERVICE
--     depends_on     anything -> anything it needs or uses
--     serves_domain  SERVICE/HOST/REPO -> DOMAIN
--     member_of      PERSON/ACCOUNT -> ORG/PROJECT
--     located_at     REPO/PROJECT/SERVICE/PATH -> PATH/HOST
--   `hosts` (HOST -> SERVICE) is NOT a predicate: it is the inverse of
--   runs_on, and the extractor flips it into runs_on so a fact is stored once.
--
-- Identity: entities.id = uuid5(NAMESPACE_OID, '<TYPE>:<name_norm>'), so the
-- same thing named in two memories (or two projects) is one node; relations
-- are scoped to the project whose memory stated them, and their id is
-- uuid5 of (source, predicate, target, project). Re-runs merge.
--
-- Facts are retired, never deleted: a relation gets retired_at / valid_until
-- when a newer memory supersedes it (single-valued predicates, same source)
-- or says it ended; an entity gets archived_at / valid_until when a memory
-- says it was retired. The walk skips both.
--
-- Every relation carries `quote`: an exact span of its source memory,
-- checked in code before the write (an edge without one is dropped).
-- Memory text therefore lives in these tables (quote, description) — the
-- same trust level as devbrain.memory itself; readers wrap it as untrusted.
--
-- New tables + functions only; no change to existing tables. Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS devbrain.entities (
    id                  UUID PRIMARY KEY,
    -- The project whose memory first named it. Entities are shared across
    -- projects; relations carry the per-project scope.
    project_id          UUID REFERENCES devbrain.projects(id) ON DELETE SET NULL,
    name                TEXT NOT NULL,
    name_norm           TEXT NOT NULL,
    -- Word tokens of name_norm: the seed lookup's index key (facts_seeds).
    name_tokens         TEXT[] GENERATED ALWAYS AS (
                            array_remove(regexp_split_to_array(
                                name_norm, '[^[:alnum:]_]+'), '')) STORED,
    type                TEXT NOT NULL CHECK (type IN (
                            'PROJECT', 'REPO', 'HOST', 'SERVICE', 'ACCOUNT',
                            'DOMAIN', 'PERSON', 'ORG', 'TOOL', 'PATH')),
    -- Conditions and notes (versions, ports, limits). Newest non-empty wins.
    description         TEXT NOT NULL DEFAULT '',
    -- memory ids that named this entity (most recent 20).
    source_refs         UUID[] NOT NULL DEFAULT '{}',
    -- Source-memory times (memory.created_at), not wall clock.
    first_seen_at       TIMESTAMPTZ NOT NULL,
    last_seen_at        TIMESTAMPTZ NOT NULL,
    -- Retirement: archived_at = when we recorded it; valid_until = the
    -- created_at of the memory that said it was retired.
    archived_at         TIMESTAMPTZ,
    valid_until         TIMESTAMPTZ,
    archived_source_ref UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT entities_type_name_unique UNIQUE (type, name_norm),
    CONSTRAINT entities_name_norm_nonempty CHECK (length(name_norm) > 0)
);

CREATE INDEX IF NOT EXISTS idx_entities_name_tokens
    ON devbrain.entities USING gin (name_tokens);

CREATE TABLE IF NOT EXISTS devbrain.entity_aliases (
    entity_id   UUID NOT NULL REFERENCES devbrain.entities(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    alias_norm  TEXT NOT NULL CHECK (length(alias_norm) > 0),
    alias_tokens TEXT[] GENERATED ALWAYS AS (
                    array_remove(regexp_split_to_array(
                        alias_norm, '[^[:alnum:]_]+'), '')) STORED,
    source_ref  UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, alias_norm)
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_tokens
    ON devbrain.entity_aliases USING gin (alias_tokens);

CREATE TABLE IF NOT EXISTS devbrain.entity_relations (
    id                  UUID PRIMARY KEY,
    source_id           UUID NOT NULL REFERENCES devbrain.entities(id) ON DELETE CASCADE,
    target_id           UUID NOT NULL REFERENCES devbrain.entities(id) ON DELETE CASCADE,
    predicate           TEXT NOT NULL CHECK (predicate IN (
                            'runs_on', 'deploys_to', 'owned_by', 'uses_account',
                            'part_of', 'depends_on', 'serves_domain', 'member_of',
                            'located_at')),
    project_id          UUID NOT NULL REFERENCES devbrain.projects(id) ON DELETE CASCADE,
    -- The memory (devbrain.memory.id) the newest statement came from. Loose
    -- pointer like provenance_id: no FK, so memory deletes never block.
    source_ref          UUID,
    -- Exact span of that memory stating the fact (verified before write).
    quote               TEXT NOT NULL CHECK (length(quote) > 0),
    -- created_at of the newest memory that stated it.
    observed_at         TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Retirement (NULL = live). retired_at is wall clock; valid_until is the
    -- source-memory time of the fact that retired it.
    retired_at          TIMESTAMPTZ,
    valid_until         TIMESTAMPTZ,
    retired_reason      TEXT CHECK (retired_reason IN ('superseded', 'ended')),
    retired_by          UUID REFERENCES devbrain.entity_relations(id) ON DELETE SET NULL,
    retired_source_ref  UUID,
    CONSTRAINT entity_relations_no_self_loop CHECK (source_id <> target_id),
    CONSTRAINT entity_relations_retired_consistent CHECK (
        (retired_at IS NULL) = (retired_reason IS NULL)),
    CONSTRAINT entity_relations_triple_unique
        UNIQUE (source_id, predicate, target_id, project_id)
);

-- The walk: neighbours of a frontier in both directions. Full (not partial)
-- indexes, so they also serve the ON DELETE CASCADE checks from entities.
CREATE INDEX IF NOT EXISTS idx_entity_relations_source
    ON devbrain.entity_relations (source_id, project_id);
CREATE INDEX IF NOT EXISTS idx_entity_relations_target
    ON devbrain.entity_relations (target_id, project_id);
-- The retired_by self-reference (ON DELETE SET NULL check).
CREATE INDEX IF NOT EXISTS idx_entity_relations_retired_by
    ON devbrain.entity_relations (retired_by) WHERE retired_by IS NOT NULL;
-- Facts stated by one memory (provenance lookups, re-extraction).
CREATE INDEX IF NOT EXISTS idx_entity_relations_source_ref
    ON devbrain.entity_relations (source_ref) WHERE source_ref IS NOT NULL;

COMMENT ON TABLE devbrain.entities IS
    'Facts graph nodes (migration 050). id = uuid5(TYPE:name_norm). Written by cognify entities.';
COMMENT ON TABLE devbrain.entity_relations IS
    'Facts graph edges, project-scoped, each with a verified quote from source_ref. Retired, never deleted.';
COMMENT ON TABLE devbrain.entity_aliases IS
    'Name variants that seed the facts walk.';

-- ── Seeds: entities whose name or alias appears as a whole word ────────────
-- Case-insensitive, whitespace-collapsed. "Whole word" means the match is
-- not glued to a letter, digit or underscore on either side, so names that
-- start or end with punctuation (~/Trifekta, .env) still match. Candidates
-- come from the token GIN indexes (every word of the name is a word of the
-- text, which any whole-word match implies), so the cost follows the words
-- in the text, not the size of the graph; the regex then checks order and
-- boundaries.
CREATE OR REPLACE FUNCTION devbrain.facts_seeds(p_text TEXT)
RETURNS TABLE (entity_id UUID)
LANGUAGE sql STABLE AS $$
    WITH q AS (
        SELECT t, array(SELECT DISTINCT w
                          FROM unnest(regexp_split_to_array(t, '[^[:alnum:]_]+')) w
                         WHERE w <> '') AS words
          FROM (SELECT regexp_replace(lower(coalesce(p_text, '')), '\s+', ' ', 'g') AS t) x
    ),
    needles AS (
        SELECT e.id, e.name_norm AS needle
          FROM devbrain.entities e, q
         WHERE e.name_tokens <@ q.words
           AND cardinality(e.name_tokens) > 0
           AND e.archived_at IS NULL
           AND length(e.name_norm) >= 2
        UNION ALL
        SELECT a.entity_id, a.alias_norm
          FROM devbrain.entity_aliases a
          JOIN devbrain.entities e ON e.id = a.entity_id, q
         WHERE a.alias_tokens <@ q.words
           AND cardinality(a.alias_tokens) > 0
           AND e.archived_at IS NULL
           AND length(a.alias_norm) >= 2
    )
    SELECT DISTINCT n.id
      FROM needles n, q
     WHERE strpos(q.t, n.needle) > 0
       AND q.t ~ ('(^|[^[:alnum:]_])'
                  || regexp_replace(n.needle, '([^[:alnum:] _])', '\\\1', 'g')
                  || '($|[^[:alnum:]_])')
$$;

-- ── Walk: breadth-first over live facts, ranked by nearness to the seeds ───
-- Level by level from the seeds (depth 0). A fact's nearness is the depth
-- of its nearer endpoint; the facts at nearness d are the live relations
-- touching a depth-d entity whose other end is at depth d or d+1 (at the
-- last level, d = hops, only facts between two depth-d entities count: both
-- ends must be reached). Each entity is expanded once, at its smallest
-- depth; archived entities are neither seeds nor walked through.
--
-- The walk stops after the first level that brings the total to p_top_k,
-- so a hub seed (hundreds of edges) costs one level, not the whole graph.
-- Ranked by nearness, then newest statement, then names; with p_project
-- NULL a triple stated in several projects is returned once (newest).
-- Deterministic; no model call.
CREATE OR REPLACE FUNCTION devbrain.facts_walk(
    p_project UUID,
    p_text    TEXT,
    p_hops    INT DEFAULT 3,
    p_top_k   INT DEFAULT 8
)
RETURNS TABLE (
    relation_id  UUID,
    source_id    UUID,
    source_name  TEXT,
    source_type  TEXT,
    predicate    TEXT,
    target_id    UUID,
    target_name  TEXT,
    target_type  TEXT,
    project_id   UUID,
    source_ref   UUID,
    quote        TEXT,
    observed_at  TIMESTAMPTZ,
    depth        INT
)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE
    v_hops     INT := greatest(0, least(coalesce(p_hops, 3), 6));
    v_top_k    INT := greatest(coalesce(p_top_k, 8), 0);
    v_max      INT := 5000;      -- node cap: nothing can blow up the walk
    v_seen     UUID[];           -- entities reached so far (all depths)
    v_older    UUID[] := '{}';   -- entities at depth < d
    v_frontier UUID[];           -- entities at depth d
    v_next     UUID[];
    v_lvl      UUID[];
    v_rels     UUID[] := '{}';   -- collected relation ids
    v_near     INT[]  := '{}';   -- their nearness
    v_found    INT := 0;
    d          INT;
BEGIN
    IF v_top_k = 0 THEN
        RETURN;
    END IF;
    SELECT coalesce(array_agg(s.entity_id ORDER BY s.entity_id), '{}')
      INTO v_frontier
      FROM devbrain.facts_seeds(p_text) s;
    IF cardinality(v_frontier) = 0 THEN
        RETURN;
    END IF;
    v_seen := v_frontier;

    FOR d IN 0..v_hops LOOP
        SELECT coalesce(array_agg(x.rid), '{}'),
               coalesce(array_agg(DISTINCT x.other)
                        FILTER (WHERE NOT (x.other = ANY (v_seen))), '{}')
          INTO v_lvl, v_next
          FROM (
                SELECT DISTINCT ON (i.rid) i.rid, i.other
                  FROM (
                        SELECT r.id AS rid, r.target_id AS other
                          FROM devbrain.entity_relations r
                         WHERE r.source_id = ANY (v_frontier)
                           AND r.retired_at IS NULL
                           AND (p_project IS NULL OR r.project_id = p_project)
                        UNION ALL
                        SELECT r.id, r.source_id
                          FROM devbrain.entity_relations r
                         WHERE r.target_id = ANY (v_frontier)
                           AND r.retired_at IS NULL
                           AND (p_project IS NULL OR r.project_id = p_project)
                       ) i
                  JOIN devbrain.entities e ON e.id = i.other AND e.archived_at IS NULL
                 WHERE NOT (i.other = ANY (v_older))
                   AND (d < v_hops OR i.other = ANY (v_frontier))
                 ORDER BY i.rid
               ) x;

        v_rels := v_rels || v_lvl;
        v_near := v_near || array_fill(d, ARRAY[cardinality(v_lvl)]);
        IF p_project IS NULL THEN
            SELECT count(DISTINCT (r.source_id, r.predicate, r.target_id))
              INTO v_found
              FROM devbrain.entity_relations r WHERE r.id = ANY (v_rels);
        ELSE
            v_found := cardinality(v_rels);
        END IF;

        EXIT WHEN v_found >= v_top_k
               OR d >= v_hops
               OR cardinality(v_next) = 0
               OR cardinality(v_seen) >= v_max;
        v_older := v_seen;
        v_seen := v_seen || v_next;
        v_frontier := v_next;
    END LOOP;

    RETURN QUERY
    WITH col AS (
        SELECT u.rid, u.near FROM unnest(v_rels, v_near) AS u(rid, near)
    ),
    hits AS (
        SELECT DISTINCT ON (r.source_id, r.predicate, r.target_id)
               r.id AS rid, r.source_id AS sid, r.target_id AS tid,
               r.predicate AS pred, r.project_id AS pid, r.source_ref AS sref,
               r.quote AS q, r.observed_at AS obs, c.near
          FROM col c
          JOIN devbrain.entity_relations r ON r.id = c.rid
         ORDER BY r.source_id, r.predicate, r.target_id, r.observed_at DESC, r.id
    )
    SELECT h.rid, h.sid, es.name, es.type, h.pred, h.tid, et.name, et.type,
           h.pid, h.sref, h.q, h.obs, h.near
      FROM hits h
      JOIN devbrain.entities es ON es.id = h.sid
      JOIN devbrain.entities et ON et.id = h.tid
     ORDER BY h.near, h.obs DESC, es.name, h.pred, et.name, h.rid
     LIMIT v_top_k;
END;
$$;

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('050_entity_facts_graph.sql')
ON CONFLICT (filename) DO NOTHING;

COMMIT;

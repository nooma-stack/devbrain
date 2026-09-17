\set ON_ERROR_STOP on

-- Build a Nooma-only migration database from a restored DevBrain dump.
--
-- Safety properties:
--   * refuses to run unless the database name has the dedicated staging prefix
--   * retains only the explicit Nooma project allowlist
--   * recognizes orphan sessions only when their encoded Claude path is an
--     unambiguous DevBrain/DevBrain Factory path
--   * rejects BrightBot source paths even if historical attribution assigned
--     them to an allowed project
--   * disables audit triggers during filtering, then prunes and rechains the
--     retained ledger before enabling the triggers again

BEGIN;

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '0';

DO $$
BEGIN
    IF current_database() !~ '^devbrain_nooma_stage_[0-9]{8}(_[0-9]+)?$' THEN
        RAISE EXCEPTION
            'Refusing to sanitize database %; expected devbrain_nooma_stage_YYYYMMDD[_N]',
            current_database();
    END IF;
END
$$;

CREATE TEMP TABLE _nooma_allowed_projects (
    slug text PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO _nooma_allowed_projects (slug) VALUES
    ('50tel-pbx'),
    ('agentbeacon'),
    ('amelia-rose-art'),
    ('amios'),
    ('cvportal'),
    ('cvportal-pre-rename'),
    ('deallayer'),
    ('devbrain'),
    ('home-patrickkelly'),
    ('patrick-life-ops'),
    ('pkrelay'),
    ('soho-reboot');

DO $$
BEGIN
    IF (SELECT count(*) FROM devbrain.projects WHERE slug = 'devbrain') <> 1 THEN
        RAISE EXCEPTION 'Expected exactly one devbrain project before sanitization';
    END IF;
END
$$;

-- Attribute only the two path families that are unambiguously Nooma/DevBrain.
UPDATE devbrain.raw_sessions
SET project_id = (SELECT id FROM devbrain.projects WHERE slug = 'devbrain')
WHERE project_id IS NULL
  AND (
      source_path LIKE '%/.claude/projects/-Users-patrickkelly-devbrain/%'
      OR source_path LIKE '%/.claude/projects/-Users-patrickkelly-devbrain-factory/%'
  );

UPDATE devbrain.chunks c
SET project_id = rs.project_id
FROM devbrain.raw_sessions rs
WHERE c.project_id IS NULL
  AND c.source_id = rs.id
  AND rs.project_id = (SELECT id FROM devbrain.projects WHERE slug = 'devbrain')
  AND (
      rs.source_path LIKE '%/.claude/projects/-Users-patrickkelly-devbrain/%'
      OR rs.source_path LIKE '%/.claude/projects/-Users-patrickkelly-devbrain-factory/%'
  );

-- This snapshot drives all dependent filtering. It deliberately includes
-- ambiguous null-project sessions and the known historical misattribution in
-- which a BrightBot transcript was assigned to the devbrain project.
CREATE TEMP TABLE _nooma_rejected_sessions ON COMMIT DROP AS
SELECT rs.id
FROM devbrain.raw_sessions rs
LEFT JOIN devbrain.projects p ON p.id = rs.project_id
WHERE rs.project_id IS NULL
   OR NOT EXISTS (
       SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug
   )
   OR lower(rs.source_path) LIKE '%brightbot%';

ALTER TABLE _nooma_rejected_sessions ADD PRIMARY KEY (id);

-- Bulk filtering is a migration operation, not a stream of user mutations.
-- Suppress audit side effects, then rebuild a valid ledger for retained rows.
ALTER TABLE devbrain.memory DISABLE TRIGGER USER;
ALTER TABLE devbrain.memory_dependencies DISABLE TRIGGER USER;

-- Ephemeral work queues do not belong in a machine migration.
DELETE FROM devbrain.curator_re_eval_queue;
DELETE FROM devbrain.refinement_queue;
DELETE FROM devbrain.file_locks;
DELETE FROM devbrain.factory_runtime_state;
DELETE FROM devbrain.notifications;
DELETE FROM devbrain.invitations;

-- Legacy knowledge tables can reference both sessions and themselves.
UPDATE devbrain.decisions d
SET superseded_by = NULL
WHERE d.superseded_by IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM devbrain.decisions doomed
      LEFT JOIN devbrain.projects p ON p.id = doomed.project_id
      WHERE doomed.id = d.superseded_by
        AND (
            NOT EXISTS (
                SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug
            )
            OR doomed.session_id IN (SELECT id FROM _nooma_rejected_sessions)
        )
  );

DELETE FROM devbrain.decisions d
USING devbrain.projects p
WHERE d.project_id = p.id
  AND (
      NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug)
      OR d.session_id IN (SELECT id FROM _nooma_rejected_sessions)
  );

DELETE FROM devbrain.patterns ptn
USING devbrain.projects p
WHERE ptn.project_id = p.id
  AND (
      NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug)
      OR ptn.session_id IN (SELECT id FROM _nooma_rejected_sessions)
  );

DELETE FROM devbrain.issues i
USING devbrain.projects p
WHERE i.project_id = p.id
  AND (
      NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug)
      OR i.session_id IN (SELECT id FROM _nooma_rejected_sessions)
  );

-- Preserve factory history for allowed projects, but remove target-machine
-- runtime state and all jobs/artifacts belonging to other organizations.
CREATE TEMP TABLE _nooma_rejected_jobs ON COMMIT DROP AS
SELECT j.id
FROM devbrain.factory_jobs j
JOIN devbrain.projects p ON p.id = j.project_id
WHERE NOT EXISTS (
    SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug
);

ALTER TABLE _nooma_rejected_jobs ADD PRIMARY KEY (id);

UPDATE devbrain.factory_jobs
SET blocked_by_job_id = NULL
WHERE blocked_by_job_id IN (SELECT id FROM _nooma_rejected_jobs);

DELETE FROM devbrain.factory_artifacts
WHERE job_id IN (SELECT id FROM _nooma_rejected_jobs);

DELETE FROM devbrain.factory_cleanup_reports
WHERE job_id IN (SELECT id FROM _nooma_rejected_jobs);

DELETE FROM devbrain.factory_jobs
WHERE id IN (SELECT id FROM _nooma_rejected_jobs);

-- Keep only memory in allowed projects whose raw-session provenance is also
-- retained. The provenance check catches legacy backfills; the fanout check
-- catches cross-project session summaries.
DELETE FROM devbrain.memory m
USING devbrain.projects p
WHERE m.project_id = p.id
  AND (
      NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug)
      OR m.fanout_source_session_id IN (
          SELECT id FROM _nooma_rejected_sessions
      )
      OR m.provenance_id IN (
          SELECT id FROM _nooma_rejected_sessions
      )
  );

DELETE FROM devbrain.chunks c
USING devbrain.projects p
WHERE c.project_id = p.id
  AND (
      NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug)
      OR c.source_id IN (SELECT id FROM _nooma_rejected_sessions)
  );

DELETE FROM devbrain.chunks
WHERE project_id IS NULL;

DELETE FROM devbrain.raw_sessions
WHERE id IN (SELECT id FROM _nooma_rejected_sessions);

-- Remaining project-scoped operational and evaluation data follows the same
-- explicit allowlist. Tables with special dependencies were handled above.
DELETE FROM devbrain.codebase_index t
USING devbrain.projects p
WHERE t.project_id = p.id
  AND NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug);

DELETE FROM devbrain.cognify_run_log t
USING devbrain.projects p
WHERE t.project_id = p.id
  AND NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug);

DELETE FROM devbrain.cognify_spend_log t
USING devbrain.projects p
WHERE t.project_id = p.id
  AND NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug);

DELETE FROM devbrain.end_session_log t
USING devbrain.projects p
WHERE t.project_id = p.id
  AND NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug);

DELETE FROM devbrain.port_assignments t
USING devbrain.projects p
WHERE t.project_id = p.id
  AND NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug);

DELETE FROM devbrain.retrieval_miss_events t
USING devbrain.projects p
WHERE t.project_id = p.id
  AND NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug);

DELETE FROM devbrain.recall_canonical_queries t
USING devbrain.projects p
WHERE t.project_id = p.id
  AND NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug);

DELETE FROM devbrain.recall_audit_snapshots t
USING devbrain.projects p
WHERE t.project_id = p.id
  AND NOT EXISTS (SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug);

DELETE FROM devbrain.projects p
WHERE NOT EXISTS (
    SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = p.slug
);

-- Retain one Nooma operator identity. Historical submitted_by strings on
-- retained factory jobs remain audit text and are not active identities.
UPDATE devbrain.retrieval_miss_events
SET dev_id = NULL
WHERE dev_id IS NOT NULL AND dev_id <> 'patrickkelly';

UPDATE devbrain.recall_canonical_queries
SET created_by = NULL
WHERE created_by IS NOT NULL AND created_by <> 'patrickkelly';

UPDATE devbrain.end_session_log
SET dev_id = NULL
WHERE dev_id IS NOT NULL AND dev_id <> 'patrickkelly';

UPDATE devbrain.devs
SET allowed_projects = ARRAY(
    SELECT p.slug::varchar
    FROM devbrain.projects p
    ORDER BY p.slug
)
WHERE dev_id = 'patrickkelly';

DELETE FROM devbrain.devs
WHERE dev_id <> 'patrickkelly';

-- Retain semantic ledger events only for retained live memories, then rebuild
-- the chain deterministically. Gaps in seq are permitted; the hashes chain
-- across the retained rows in seq order.
DELETE FROM devbrain.memory_ledger l
WHERE NOT EXISTS (SELECT 1 FROM devbrain.memory m WHERE m.id = l.memory_id)
   OR NOT EXISTS (
       SELECT 1 FROM _nooma_allowed_projects a WHERE a.slug = l.project_slug
   );

DO $$
DECLARE
    cur record;
    prev_h bytea := NULL;
    new_hash bytea;
BEGIN
    FOR cur IN
        SELECT seq, memory_id, operation, actor, project_slug, payload_hash
        FROM devbrain.memory_ledger
        ORDER BY seq
    LOOP
        new_hash := digest(
            cur.seq::text
                || '|' || cur.memory_id::text
                || '|' || cur.operation
                || '|' || cur.actor
                || '|' || cur.project_slug
                || '|' || encode(cur.payload_hash, 'hex')
                || '|' || COALESCE(encode(prev_h, 'hex'), ''),
            'sha256'
        );

        UPDATE devbrain.memory_ledger
        SET prev_hash = prev_h,
            row_hash = new_hash
        WHERE seq = cur.seq;

        prev_h := new_hash;
    END LOOP;
END
$$;

SELECT setval(
    pg_get_serial_sequence('devbrain.memory_ledger', 'seq'),
    COALESCE((SELECT max(seq) FROM devbrain.memory_ledger), 1),
    EXISTS (SELECT 1 FROM devbrain.memory_ledger)
);

ALTER TABLE devbrain.memory_dependencies ENABLE TRIGGER USER;
ALTER TABLE devbrain.memory ENABLE TRIGGER USER;

COMMIT;

\echo 'Sanitized staging counts by project:'
SELECT
    p.slug,
    (SELECT count(*) FROM devbrain.memory m WHERE m.project_id = p.id)
        AS memory_rows,
    (SELECT count(*) FROM devbrain.raw_sessions rs WHERE rs.project_id = p.id)
        AS raw_sessions
FROM devbrain.projects p
ORDER BY p.slug;

\set ON_ERROR_STOP on

-- Post-restore validation for the target database. This is intentionally
-- separate from validate-staging.sql so each file has a fail-closed database
-- name guard.
DO $$
DECLARE
    bad_count bigint;
    memory_count bigint;
    raw_count bigint;
BEGIN
    IF current_database() <> 'devbrain' THEN
        RAISE EXCEPTION 'Refusing target validation on database %', current_database();
    END IF;

    SELECT count(*) INTO bad_count
    FROM devbrain.projects
    WHERE slug NOT IN (
        '50tel-pbx', 'agentbeacon', 'amelia-rose-art', 'amios',
        'cvportal', 'cvportal-pre-rename', 'deallayer', 'devbrain',
        'home-patrickkelly', 'patrick-life-ops', 'pkrelay', 'soho-reboot'
    );
    IF bad_count <> 0 THEN
        RAISE EXCEPTION 'Found % non-Nooma projects', bad_count;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM devbrain.projects WHERE slug = 'devbrain') THEN
        RAISE EXCEPTION 'Required devbrain project is missing';
    END IF;

    SELECT count(*) INTO bad_count
    FROM devbrain.devs
    WHERE dev_id <> 'patrickkelly';
    IF bad_count <> 0
       OR (SELECT count(*) FROM devbrain.devs WHERE dev_id = 'patrickkelly') <> 1 THEN
        RAISE EXCEPTION 'Target developer identity allowlist failed';
    END IF;

    SELECT count(*) INTO bad_count
    FROM devbrain.raw_sessions
    WHERE project_id IS NULL OR lower(source_path) LIKE '%brightbot%';
    IF bad_count <> 0 THEN
        RAISE EXCEPTION 'Found % unattributed or BrightBot raw sessions', bad_count;
    END IF;

    SELECT count(*) INTO bad_count
    FROM devbrain.chunks
    WHERE project_id IS NULL;
    IF bad_count <> 0 THEN
        RAISE EXCEPTION 'Found % unattributed chunks', bad_count;
    END IF;

    SELECT count(*) INTO bad_count
    FROM devbrain.memory m
    LEFT JOIN devbrain.raw_sessions rs ON rs.id = m.fanout_source_session_id
    WHERE m.fanout_source_session_id IS NOT NULL AND rs.id IS NULL;
    IF bad_count <> 0 THEN
        RAISE EXCEPTION 'Found % dangling fanout memories', bad_count;
    END IF;

    SELECT count(*) INTO bad_count
    FROM devbrain.memory_ledger l
    LEFT JOIN devbrain.memory m ON m.id = l.memory_id
    WHERE m.id IS NULL
       OR l.project_slug NOT IN (
           '50tel-pbx', 'agentbeacon', 'amelia-rose-art', 'amios',
           'cvportal', 'cvportal-pre-rename', 'deallayer', 'devbrain',
           'home-patrickkelly', 'patrick-life-ops', 'pkrelay', 'soho-reboot'
       );
    IF bad_count <> 0 THEN
        RAISE EXCEPTION 'Found % invalid retained ledger rows', bad_count;
    END IF;

    IF EXISTS (SELECT 1 FROM devbrain.verify_chain()) THEN
        RAISE EXCEPTION 'Retained memory ledger hash chain does not verify';
    END IF;

    IF EXISTS (SELECT 1 FROM devbrain.invitations)
       OR EXISTS (SELECT 1 FROM devbrain.notifications)
       OR EXISTS (SELECT 1 FROM devbrain.file_locks)
       OR EXISTS (SELECT 1 FROM devbrain.factory_runtime_state)
       OR EXISTS (SELECT 1 FROM devbrain.curator_re_eval_queue)
       OR EXISTS (SELECT 1 FROM devbrain.refinement_queue) THEN
        RAISE EXCEPTION 'One or more ephemeral/runtime tables are not empty';
    END IF;

    SELECT count(*) INTO memory_count FROM devbrain.memory;
    SELECT count(*) INTO raw_count FROM devbrain.raw_sessions;
    IF memory_count < 30000 OR raw_count < 1000 THEN
        RAISE EXCEPTION
            'Retained dataset is unexpectedly small (memory %, raw_sessions %)',
            memory_count, raw_count;
    END IF;
END
$$;

\echo 'Nooma target validation passed.'
SELECT
    (SELECT count(*) FROM devbrain.projects) AS projects,
    (SELECT count(*) FROM devbrain.devs) AS devs,
    (SELECT count(*) FROM devbrain.memory) AS memory,
    (SELECT count(*) FROM devbrain.raw_sessions) AS raw_sessions,
    (SELECT count(*) FROM devbrain.chunks) AS chunks,
    (SELECT count(*) FROM devbrain.memory_ledger) AS ledger_rows;


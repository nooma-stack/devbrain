-- ─────────────────────────────────────────────────────────────────────────────
-- 028: Add allowed_projects to devbrain.devs
-- ─────────────────────────────────────────────────────────────────────────────
--
-- NULL  = dev can submit to any project (default, existing-dev behavior preserved).
-- '{}'  = dev is locked out of all projects.
-- '{slug1,slug2}' = dev can only submit to those slugs.
--
-- Uses project SLUGS (not UUIDs) for portability across DB instances.

ALTER TABLE devbrain.devs
    ADD COLUMN IF NOT EXISTS allowed_projects TEXT[] DEFAULT NULL;

COMMENT ON COLUMN devbrain.devs.allowed_projects IS
    'NULL = all projects allowed. Empty array = no projects. '
    'Otherwise: list of project slugs this dev may submit jobs to. '
    'Added in migration 028.';

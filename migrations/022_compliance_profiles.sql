-- Atlas Step 7 — Per-project compliance profiles
-- ============================================================================
--
-- Adds the substrate for filtering rules by compliance profile membership.
-- Per the playbook §5 + DevBrain decision fc1a62bb (Option A locked):
--
--   1. devbrain.memory.compliance_profiles text[] — rules tag themselves
--      with one or more profiles ('hipaa', 'soc2', 'ferpa', etc.). NULL/[]
--      means the rule applies to NO project (explicit opt-in semantics).
--   2. devbrain.projects.compliance_profiles_enabled text[] — projects
--      enable specific profiles. Curator brief filters rules by intersection.
--   3. GIN index on memory.compliance_profiles for the curator hot-path
--      query: WHERE compliance_profiles && %s::text[]

ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS compliance_profiles TEXT[];

ALTER TABLE devbrain.projects
    ADD COLUMN IF NOT EXISTS compliance_profiles_enabled TEXT[];

CREATE INDEX IF NOT EXISTS idx_memory_compliance_profiles_gin
    ON devbrain.memory USING GIN (compliance_profiles)
    WHERE compliance_profiles IS NOT NULL;

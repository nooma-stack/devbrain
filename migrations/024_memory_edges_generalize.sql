-- ═══════════════════════════════════════════════════════════════════
-- 024: Generalize memory_dependencies for Phase 5 graph layer
-- ═══════════════════════════════════════════════════════════════════
--
-- Phase 5 (graph layer) extends memory_dependencies from 4 edge types
-- to 6 by relaxing the CHECK constraint. No table rename, no column
-- additions — the existing shape is already the general edge model.
--
--   - 'derived_from'  → from extracted from to (lessons from sessions)
--   - 'refined_by'    → to is a sharpening/elaboration of from

ALTER TABLE devbrain.memory_dependencies
    DROP CONSTRAINT IF EXISTS memory_dependencies_edge_type_check;

ALTER TABLE devbrain.memory_dependencies
    ADD CONSTRAINT memory_dependencies_edge_type_check
    CHECK (edge_type IN (
        'cites', 'depends_on', 'supersedes', 'contradicts',
        'derived_from', 'refined_by'
    ));

-- Track this migration in schema_migrations.
INSERT INTO devbrain.schema_migrations (filename, applied_at)
VALUES ('024_memory_edges_generalize.sql', now())
ON CONFLICT (filename) DO NOTHING;

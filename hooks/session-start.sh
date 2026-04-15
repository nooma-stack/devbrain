#!/bin/bash
# DevBrain SessionStart hook
# Queries DevBrain for project context and injects it into the conversation.
# Called automatically by Claude Code on every session start/resume.

PROJECT="${DEVBRAIN_PROJECT:-}"
DB_URL="${DEVBRAIN_DATABASE_URL:-postgresql://devbrain:devbrain-local@localhost:5433/devbrain}"

if [ -z "$PROJECT" ]; then
    echo "DevBrain: No DEVBRAIN_PROJECT set; skipping context load."
    exit 0
fi

# Quick DB query for project context (no MCP needed, direct SQL)
CONTEXT=$(psql "$DB_URL" -t -A -c "
SELECT json_build_object(
    'project', (SELECT json_build_object('name', name, 'description', description, 'constraints', constraints)
                FROM devbrain.projects WHERE slug = '$PROJECT'),
    'recent_decisions', (SELECT coalesce(json_agg(json_build_object('title', title, 'decision', decision)), '[]'::json)
                         FROM (SELECT title, decision FROM devbrain.decisions
                               WHERE project_id = (SELECT id FROM devbrain.projects WHERE slug = '$PROJECT')
                               AND status = 'active' ORDER BY created_at DESC LIMIT 3) d),
    'recent_issues', (SELECT coalesce(json_agg(json_build_object('title', title, 'fix', fix_applied)), '[]'::json)
                      FROM (SELECT title, fix_applied FROM devbrain.issues
                            WHERE project_id = (SELECT id FROM devbrain.projects WHERE slug = '$PROJECT')
                            ORDER BY created_at DESC LIMIT 3) i),
    'active_jobs', (SELECT coalesce(json_agg(json_build_object('title', title, 'status', status, 'phase', current_phase)), '[]'::json)
                    FROM (SELECT title, status, current_phase FROM devbrain.factory_jobs
                          WHERE project_id = (SELECT id FROM devbrain.projects WHERE slug = '$PROJECT')
                          AND status NOT IN ('approved','rejected','deployed','failed')
                          ORDER BY created_at DESC LIMIT 3) j)
);" 2>/dev/null)

if [ -n "$CONTEXT" ] && [ "$CONTEXT" != "null" ]; then
    echo "DevBrain context loaded for project: $PROJECT"
    echo "$CONTEXT" | python3 -m json.tool 2>/dev/null || echo "$CONTEXT"
else
    echo "DevBrain: No project context available (DB may not be running)"
fi

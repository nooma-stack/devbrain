#!/bin/bash
# SessionStart(compact) hook: right after a compaction, the model still
# holds the freshly-written compaction summary — the perfect moment to
# persist a devbrain checkpoint before that arc fades into paraphrase.
#
# Install (per machine, additive):
#   mkdir -p ~/.claude/hooks && cp devbrain-checkpoint.sh ~/.claude/hooks/ && chmod +x ~/.claude/hooks/devbrain-checkpoint.sh
#   then merge the snippet from settings-snippet.json into ~/.claude/settings.json
#
# The hook only fires on source == "compact" (not startup/resume/clear),
# and only ADDS context — it never blocks the session.

input=$(cat)
source=$(printf '%s' "$input" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p' | head -1)

if [ "$source" = "compact" ]; then
  cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Your context was just compacted. Before continuing the task, persist a checkpoint of the compacted arc: call the devbrain (or brightbrain, per this project's CLAUDE.md) `breadcrumb` tool ONCE with title starting 'CHECKPOINT pre-compact:' and a structured body — accomplishments, decisions made, open threads, and the immediate next step. Reuse your saved conversation_uuid if you have one. Keep it under 400 words, then continue the task without further comment about the checkpoint."}}
JSON
fi
exit 0

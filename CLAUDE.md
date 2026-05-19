# DevBrain Integration — devbrain

## DevBrain — Persistent Memory (IMPORTANT)

You have access to DevBrain, a shared persistent memory system via MCP tools.
DevBrain remembers across sessions and across different AI tools.

### MCP routing (THIS project)

This project's memory lives in the **local DevBrain on Patrick's laptop**
(`mcp__devbrain__*`). NOT BrightBrain — that's the LHT Mac Studio for
BrightBot / lht-vps / brightbot-v4 work only.

- Default `project` argument: `devbrain` (the MCP normalizes; the DB-visible
  display name is `DevBrain` but the MCP accepts the lowercase form).
- Examples: `mcp__devbrain__get_project_context`, `mcp__devbrain__deep_search`,
  `mcp__devbrain__store`, `mcp__devbrain__end_session`,
  `mcp__devbrain__factory_plan`, `mcp__devbrain__factory_status`.

If you find yourself accidentally calling `mcp__brightbrain__*` while in
this repo, you're hitting the wrong DB. Stop and re-route to `mcp__devbrain__*`.

### ALWAYS do these things:
1. **Start of session — load context**: Call `get_project_context` (via `mcp__devbrain__*`) to load project context, recent decisions, and known issues.
2. **Start of session — anchor the chain**: After reading the user's first prompt, call `start_session(project, intent)` where `intent` is a 1-3 sentence paraphrase of what the user is asking. **Save the returned `conversation_uuid`** in your working memory — you'll reuse it on every subsequent breadcrumb + end_session call so the whole conversation chains together.
3. **Before starting work**: Call `deep_search` (via `mcp__devbrain__*`) to check for relevant past decisions, patterns, and implementation history — never assume
4. **During work — atomic memories**: Call `store` (via `mcp__devbrain__*`) when you:
   - Make an architecture or design decision (type="decision")
   - Discover a reusable pattern (type="pattern")
   - Fix a bug worth remembering (type="issue")
5. **During work — narrative breadcrumbs**: Call `breadcrumb(project, conversation_uuid, title, content)` at meaningful milestones in long sessions — a decision settled, a problem solved, a direction change, completion of a sub-task in an autonomous run. Use the `conversation_uuid` you saved in step 2.
6. **End of session**: Call `end_session(project, session_id=conversation_uuid, summary, ...)` with a summary of what was accomplished, decisions made, files changed, and next steps. Pass the same `conversation_uuid` as `session_id` so the final summary chains to the intent + breadcrumbs.

If you forget step 2 and call `breadcrumb` first, the server auto-generates a `conversation_uuid` and returns it — reuse that for the remaining calls. The intent anchor (seq=0) is lost in that path, so calling `start_session` explicitly is strongly preferred.

### DevBrain Search Tips:
- `deep_search` with depth="auto" will automatically fetch raw transcript context for high-confidence matches
- Use `get_source_context(chunk_id)` to drill into raw transcripts when you need more detail
- Search cross-project with `cross_project=true` when the question spans multiple projects

### Dev Factory:
- `factory_plan` — Submit a feature for autonomous implementation
- `factory_status` — Check job progress
- `factory_approve` — Approve/reject completed factory jobs

### Project: devbrain
All DevBrain tools default to this project. No need to specify `project` parameter.
